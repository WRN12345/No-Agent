# No-Agent —— 自进化 Agent 框架

能用脚本就用脚本，脚本坏了 AI 接管并顺手修好，没见过的任务 AI 自己做、做多了就沉淀成脚本。

```
任务 ─► 匹配技能 ──命中──► 执行脚本 ──成功──► 完成            （最快、最省：一次小模型调用）
          │                    │
          │                    └─第 k 步失败─► AI 从第 k 步接管 ─► 完成
          │                                         └─► [后台] 参考接管过程只改第 k 步 → 回归测试 → 自动上线 v(n+1)
          │
          └─未命中──► AI 自由执行（只能用工具）─► 完成
                                    └─► [后台] 积累 N 条同类轨迹 → 提炼参数化技能 → 回放测试 → 自动上线
```

## 安装

```bash
pip install No-Agent              # 核心库
pip install "No-Agent[server]"    # 附带 HTTP 服务（FastAPI）
pip install "No-Agent[browser]"   # 附带浏览器工具（Playwright，默认使用本机 Chrome）
```

本仓库开发：`cd backend && pip install -e ".[dev]" && python -m pytest`（离线跑通完整进化闭环，使用假 LLM）。

## 作为库使用

```python
from no_agent import Agent, AgentConfig, LLMConfig, tool

@tool(
    "query_orders",
    description="按状态查询订单",
    parameters={"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
)
async def query_orders(status: str) -> list[dict]:
    ...

agent = Agent(
    AgentConfig(llm=LLMConfig(api_key="...", fast_model="gpt-4o-mini", strong_model="gpt-4o")),
    tools=[query_orders],
)

async with agent:                                  # 启动后台进化器；退出时等待进化任务完成
    run = await agent.run("查一下所有待发货的订单")
    print(run.mode, run.result)                    # mode: skill / skill_takeover / guide / freeform / aborted

run = agent.run_sync("查一下所有待发货的订单")      # 同步代码中使用
```

也可以从环境变量读配置：`Agent(AgentConfig.from_env())`，变量见 [.env.example](.env.example)（前缀 `NO_AGENT_`）。

### 显式调用技能

自然语言 `agent.run(...)` 每次都要先匹配技能。已经知道要用哪个技能时，直接调用更快，行为也完全可预期：

```python
run = await agent.skill("fill-form").run({"url": "https://...", "data": {"姓名": "张三"}})  # 跳过匹配，零匹配开销
run = await agent.skill("fill-form").run(task="帮我填 https://... 我叫张三")              # 只给任务：小模型抽取参数
```

`params` 会按技能的 `params_schema` 校验（必填项、类型），不合法直接抛 `ValueError`，不会带着错误参数开跑。

## 技能格式（兼容 Anthropic Agent Skills）

每个技能是一个目录，**SKILL.md 是标准格式**，可以原样放进 Claude Code、Codex 等工具使用：

```
fill-form/
  SKILL.md            标准 frontmatter（name / description）+ 正文：给 AI 或人看的操作指南
  no-agent.json       本框架的扩展数据：参数 schema、版本历史、状态、权限声明
  scripts/flow_v1.py  流程脚本（可直接执行；修补后产生 flow_v2.py ...）
  tests/cases.json    回归用例（录制的工具调用与问答）
```

两种技能：

| 类型 | 构成 | 命中后 |
|---|---|---|
| **流程型**（flow） | SKILL.md + no-agent.json + scripts | 直接执行脚本；失败时 AI 从失败步骤接管，后台修补 |
| **指南型**（guide） | 只有 SKILL.md（例如第三方的 Anthropic Skill） | 把正文注入 AI 的提示词，AI 照着做（`mode="guide"`） |

```python
agent.install_skill("path/to/some-skill")          # 安装（复制到本项目，先校验 SKILL.md 和所有脚本）
AgentConfig(extra_skill_dirs=["/team/skills"])     # 或以只读方式挂载团队 / 第三方技能库（git 仓库即可）
```

- 写回 SKILL.md 时只更新 `name` / `description`，**保留其他 frontmatter 字段和正文**
- 提炼新技能时，强模型会同时写出正文指南，所以学到的技能脱离本框架也能被其他 Agent 使用
- 旧格式（`manifest.json` + `vN.py`）加载时自动迁移

## 浏览器与人机交互

不走"截图 → 看图 → 移鼠标"的 computer use，而是读元素树、批量操作：

```python
from no_agent import Agent, AgentConfig, BrowserConfig

agent = Agent(AgentConfig(browser=BrowserConfig(enabled=True)))   # 框架自己启动本机 Chrome
async with agent:
    run = await agent.run("在 https://example.com/apply 填写报名表，我叫张三，技术部")
```

运行示例：`python examples/fill_form.py <网址> "要填写的信息"`

**内置 `fill_form` 技能**（开启浏览器后自动加载，可被修补、可在本地覆盖）：

```
open_page → snapshot（一次读出全部字段：标签/类型/必填/选项/稳定选择器）
          → map_values（小模型把已知信息对应到字段，只填有依据的）
          → ask_missing（缺失的必填项在终端里一次问完）
          → fill（批量填写）→ check（回读校验，不一致就失败、交给 AI 接管）
          → submit（框架强制请求确认，确认内容里带填写摘要）
```

**浏览器工具**：`browser_open` / `browser_snapshot` / `browser_fill` / `browser_read` / `browser_click` / `browser_submit` / `browser_text`。提交按钮只能通过 `browser_submit`，`browser_click` 会拒绝点击提交按钮。

**提问与确认**：

- 脚本里用 `await ctx.ask([...])` / `await ctx.confirm("...")`，AI 自由执行时用 `ask_user` 工具，都要求**一次问完**
- 工具声明 `confirm=True`（如 `browser_submit`）后，执行前由框架强制确认，不依赖脚本或模型自觉；默认每次都问，`auto_confirm=True` 可关闭
- 用户拒绝确认或输入 `:q` 取消 → `mode="aborted"`：不算脚本失败，不触发接管 / 修补，不计入技能统计
- 等待用户输入的时间不计入步骤超时
- 问答会录进轨迹，提炼和测试回放时直接用录制的回答，不会再弹问题，也不会真的提交
- 提问方式可替换：`Agent(asker=...)`，实现 `ask()` / `confirm()` 即可（默认终端；非交互环境为 None，此时需确认的操作一律拒绝）。`ScriptedAsker` 用于测试和无人值守的批处理

## 数据存在哪

框架不替你决定，但默认值是安全的：

```
<你的项目>/
  .no-agent/
    skills/        学到的技能 —— 建议提交到 git（可审查、可回滚、团队共享）
    runs.db        运行轨迹   —— 不提交
    workspace/     工具工作区 —— 不提交
    .stats/        技能运行统计 —— 不提交
    .gitignore     自动生成，已按上面的规则配置
```

**`.no-agent/` 的位置**按以下顺序确定（和 git 找 `.git/` 一样，在子目录启动不会另起一份数据）：

1. `AgentConfig(home=...)`
2. 环境变量 `NO_AGENT_HOME`
3. 从当前目录向上查找已有的 `.no-agent/`
4. 在当前目录创建

**单独覆盖**：

| 配置 | 默认 | 说明 |
|---|---|---|
| `skills_dir` | `<home>/skills` | 可写技能层，进化只写这里 |
| `extra_skill_dirs` | `[]` | 只读技能层（团队共享库），同 id 时本地覆盖共享 |
| `database_url` | `sqlite:///<home>/runs.db` | 任意 SQLAlchemy URL；表名带 `no_agent_` 前缀，可直接放进已有数据库 |
| `workspace_dir` | `<home>/workspace` | 文件工具的沙箱目录 |

**完全自定义存储**：实现 `SkillStore` / `RunStore` 协议（[storage/base.py](src/no_agent/storage/base.py)），传给 `Agent(skill_store=..., run_store=...)`。

**分层与写时复制**：只读层的技能被修补、切换版本或下线时，会先整体复制到可写层再修改，共享库本身不会被改动。只是"用了一下"只记统计，不会触发复制。

**结构版本**：技能目录（当前 v2）和运行记录库（当前 v1）各自记录 `schema_version`，分别迁移：技能旧格式在加载时自动改写，数据库迁移登记在 `storage/sql.py` 的 `MIGRATIONS` 中、启动时执行。遇到比框架更新的数据会拒绝加载，避免旧版本写坏新数据。

## 作为服务使用

```python
# 挂到已有的 FastAPI 应用
from no_agent.server import create_router
app.include_router(create_router(agent), prefix="/agent")
```

```bash
# 独立运行（从 NO_AGENT_* 环境变量读配置）
uvicorn no_agent.server:app
docker compose up                  # 见 ../docker-compose.yml，数据在 /data 卷中
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/run` | 执行任务；`mode=freeform` 强制 AI 做，`mode=skill` + `skill_id` 指定技能 |
| GET | `/api/v1/runs`、`/api/v1/runs/{id}` | 运行记录与完整轨迹 |
| POST | `/api/v1/feedback` | 反馈 `{run_id, score: -1~1, comment}` |
| GET | `/api/v1/skills`、`/api/v1/skills/{id}` | 技能列表 / 详情（含类型、所在层、指南正文、脚本源码） |
| POST | `/api/v1/skills/{id}/run` | 显式调用技能 `{params}` 或 `{task}`，参数不合法返回 422 |
| POST | `/api/v1/skills/{id}/activate` | 切换 / 回滚版本 `{version}` |
| POST | `/api/v1/skills/{id}/retire`，DELETE `/api/v1/skills/{id}` | 下线 / 删除（只读层技能不能删） |
| GET | `/api/v1/evolver` | 后台进化任务队列与历史（含每次进化消耗的 token） |
| GET | `/api/v1/tools` | 可用工具 |

## 代码结构

| 路径 | 作用 |
|---|---|
| `src/no_agent/agent.py` | 对外入口 `Agent`：装配、生命周期、`run` / `run_sync` |
| `src/no_agent/config.py` | `AgentConfig` / `LLMConfig` |
| `src/no_agent/engine/agent.py` | 主流程：匹配 → 执行 → 接管 → 记录 → 投递进化任务 |
| `src/no_agent/engine/freeform.py` | 自由执行 / 接管（工具调用循环） |
| `src/no_agent/engine/evolver.py` | 后台进化器：提炼（distill）、修补（patch）、反馈修补（feedback） |
| `src/no_agent/engine/testing.py` | 上线门禁：录制回放测试 |
| `src/no_agent/skills/skillmd.py` | SKILL.md 解析、校验、渲染 |
| `src/no_agent/skills/script.py` | 脚本格式、AST 安全校验、按函数修补、`SCRIPT_API_DOC` |
| `src/no_agent/skills/runtime.py` | 逐步执行器，精确报告失败步骤、错误行号、当时状态 |
| `src/no_agent/skills/matcher.py` | 本地召回（向量 / 词法）+ 小模型判定与参数抽取 |
| `src/no_agent/storage/` | 存储协议、分层文件技能库、SQL 运行记录、home 解析 |
| `src/no_agent/tools/` | 工具层；内置 calculator / http_request / 文件读写 / current_time；`browser.py` 浏览器工具 |
| `src/no_agent/interact.py` | 提问与确认：`Asker` 协议、终端实现、`ask_user` 工具 |
| `src/no_agent/builtin_skills/` | 随框架分发的只读技能（`fill-form`） |
| `src/no_agent/server/` | 可选 HTTP 服务 |

## 关键设计

- **技能 = SKILL.md 指南 + 脚本 + 参数 schema + verify + 回归用例 + 版本历史**。全部是人类可读的文件，可以直接手改。
- **步骤是修补的最小单位**。失败时只把出错的步骤、错误、状态、接管过程交给强模型，只重写那一个函数。
- **verify 防止"静默错误"**。脚本成功但 verify 不通过同样触发接管；用户负反馈会触发修补并加强 verify。
- **测试门禁才自动上线**：新版本必须通过新用例，且历史用例通过率 ≥ `promote_min_pass_rate`；不通过会带着失败信息重试，仍不过则不上线。
- **录制回放**：测试时参数一致的工具调用直接回放录制结果，有副作用的调用不执行（dry run）。工具务必正确标注 `side_effect`。
- **模型分层**：高频的匹配用 fast 模型，低频的提炼/修补用 strong 模型；技能库为空或无召回候选时不调用匹配模型。
- **混合脚本**：确定的部分写成代码，需要理解的部分用 `ctx.llm()` 调小模型。
- **自动下线**：当前版本最近 `retire_window` 次中脚本独立成功率低于阈值 → `retired`；可回滚 / 重新启用。

## 已知限制 / 后续方向

- **沙箱不是强隔离**：AST 白名单 + 受限 builtins 能挡住常见误用，但不是安全边界。工具能触及敏感系统时，应把脚本执行放到独立进程 / 容器。
- **单进程**：进化器是进程内 asyncio 队列，文件技能库用进程内锁。多进程 / 多实例部署需要外部队列和带锁的存储（可通过自定义 `SkillStore` 实现）。
- **命名空间（多租户）**：尚未支持，计划后续加入，届时通过 `schema_version` 迁移已有数据。
- **共享技能的上游更新**：技能一旦复制到本地，本地版本优先，不会自动合并上游更新。
- **浏览器**：每个 Agent 共用一个页面，暂不支持多标签页、文件上传、验证码；`run_sync` 每次调用结束都会关闭浏览器。
- **权限声明**：`no-agent.json` 的 `permissions` 目前只是声明，尚未强制检查（阶段 2）。

## 路线图（方案二：分层编译）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | SKILL.md 格式适配、指南型技能、安装、显式调用 | ✅ 完成 |
| 2 | 前置检查（guard）、影子运行后自动转正、权限强制检查 | 待做 |
| 3 | MCP 服务端 / 客户端、接入 browser-use 作为 AI 浏览层 | 待做 |
| 4 | L1 原子技能层、按热度升级（指南 → 脚本）、多候选提炼、git 技能仓库 + 签名 | 待做 |
