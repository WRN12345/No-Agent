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
pip install "No-Agent[mcp]"       # MCP 服务端 / 客户端
pip install "No-Agent[browser-use]"  # browser-use 浏览器 Agent（开放式网页探索）
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

**browser-use（可选）**：`BrowserConfig(browser_use=True)` 后多一个 `browser_agent` 工具。它把开放式的网页子任务（登录、搜索、翻页、在菜单里找页面）交给 browser-use，在**同一个浏览器**里通过 CDP 完成；完成后 `browser_snapshot` / `browser_fill` 等确定性工具在它停下的页面上接着操作。

- 分工：browser-use 负责"把页面带到正确位置"（每步都要大模型推理，贵、不确定）；可重复的部分交给确定性工具，才能沉淀成省 token 的技能
- 安全：browser-use 的点击不经过本框架的提交确认，所以**调用 browser_agent 本身默认需要用户确认**，并要求它不要提交、付款、删除
- 已在 browser-use 1.0.6 上验证接入与断开（断开后浏览器和页面保持可用）

**提问与确认**：

- 脚本里用 `await ctx.ask([...])` / `await ctx.confirm("...")`，AI 自由执行时用 `ask_user` 工具，都要求**一次问完**
- 工具声明 `confirm=True`（如 `browser_submit`）后，执行前由框架强制确认，不依赖脚本或模型自觉；默认每次都问，`auto_confirm=True` 可关闭
- 用户拒绝确认或输入 `:q` 取消 → `mode="aborted"`：不算脚本失败，不触发接管 / 修补，不计入技能统计
- 等待用户输入的时间不计入步骤超时
- 问答会录进轨迹，提炼和测试回放时直接用录制的回答，不会再弹问题，也不会真的提交
- 提问方式可替换：`Agent(asker=...)`，实现 `ask()` / `confirm()` 即可（默认终端；非交互环境为 None，此时需确认的操作一律拒绝）。`ScriptedAsker` 用于测试和无人值守的批处理

## MCP：被其他 Agent 调用，也调用别人的工具

**作为 MCP 服务**（Claude Desktop / Claude Code / Cursor 等都能接入）：

```json
{"mcpServers": {"no-agent": {"command": "no-agent", "args": ["mcp"], "env": {"NO_AGENT_HOME": "/path/to/project/.no-agent"}}}}
```

| 暴露内容 | 说明 |
|---|---|
| `run_task(task)` | 执行任意任务（自动选择 技能脚本 / AI 接管 / AI 自由执行） |
| `list_skills()` | 列出技能 |
| `skill_<id>(...)` | 每个流程型技能一个工具，参数就是技能的 `params_schema`（显式调用，零匹配开销）；按技能权限标注 `readOnlyHint` / `destructiveHint` |
| 资源 `skill://<id>/SKILL.md` | 每个技能的 SKILL.md，客户端可以直接读来当指南 |

技能里的 `ctx.ask()` 和需要确认的操作，会变成 **MCP elicitation**，由客户端弹表单问用户。客户端拒绝或不支持 elicitation 时，运行以 `aborted` 结束，绝不会跳过确认。

代码里使用：`create_mcp_server(agent)` / `await serve_stdio(agent)`（[integrations/mcp_server.py](src/no_agent/integrations/mcp_server.py)）。

**作为 MCP 客户端**：外部 MCP 服务的工具注册为本地工具（`<name>__<工具名>`），和内置工具一样被录制、回放、提炼进技能：

```python
from no_agent.config import McpServerConfig

AgentConfig(mcp_servers=[
    McpServerConfig(name="github", command="npx", args=["-y", "@modelcontextprotocol/server-github"]),
    McpServerConfig(name="crm", url="https://crm.example.com/mcp", headers={"Authorization": "Bearer ..."}),
])
```

MCP 工具注解的映射：`readOnlyHint=true` → 无副作用；其他 → 有副作用（回放测试时不执行）；`destructiveHint=true` → 执行前请求确认。

## 命令行

```bash
no-agent mcp                 # 启动 MCP 服务（stdio）
no-agent serve --port 8000   # 启动 HTTP 服务
no-agent skills              # 列出技能（含候选版本、成功率）
no-agent install <目录>      # 安装技能
```

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
| POST | `/api/v1/skills/{id}/promote`、`/reject` | 手动让候选版本转正 / 淘汰 |
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
| `src/no_agent/integrations/` | MCP 服务端 / 客户端、browser-use |
| `src/no_agent/cli.py` | 命令行 `no-agent` |

## 关键设计

- **技能 = SKILL.md 指南 + 脚本 + 参数 schema + verify + 回归用例 + 版本历史**。全部是人类可读的文件，可以直接手改。
- **步骤是修补的最小单位**。失败时只把出错的步骤、错误、状态、接管过程交给强模型，只重写那一个函数。
- **verify 防止"静默错误"**。脚本成功但 verify 不通过同样触发接管；用户负反馈会触发修补并加强 verify。
- **两道门禁才上线**：
  1. **回归测试**：新版本必须通过新用例，且历史用例通过率 ≥ `promote_min_pass_rate`；不通过会带着失败信息重试
  2. **影子运行**：通过测试的新版本先成为候选，不对外生效。之后每次真实运行结束，后台用这次的录制数据跑候选（不执行副作用、不弹问题），与真实交付的结果比对：一致直接算通过，不一致由小模型判断是否等价。一致 `shadow_min_runs` 次自动转正，失败超过 `shadow_max_failures` 次淘汰。淘汰原因会告诉下一次修补，避免重复犯错。影子运行在后台，不增加用户等待时间
- **前置检查（guard）**：脚本可定义 `guard(ctx)`，在任何步骤之前确认环境符合假设；不通过时一步都不执行，直接交给 AI 接管（避免在错误的环境里做了一半有副作用的操作）。guard 中只能调用只读工具
- **权限**：`no-agent.json` 中声明 `permissions.tools` / `domains` 后，脚本运行时只能调用声明过的工具、访问声明过的域名（AI 接管不受限）。修补不能扩大权限，用了未声明工具的补丁会被打回；安装时也做静态检查。新提炼的技能会按实际使用自动生成权限声明
- **录制回放**：测试时参数一致的工具调用直接回放录制结果，有副作用的调用不执行（dry run）。工具务必正确标注 `side_effect`。
- **模型分层**：高频的匹配用 fast 模型，低频的提炼/修补用 strong 模型；技能库为空或无召回候选时不调用匹配模型。
- **混合脚本**：确定的部分写成代码，需要理解的部分用 `ctx.llm()` 调小模型。
- **自动下线**：当前版本最近 `retire_window` 次中脚本独立成功率低于阈值 → `retired`；可回滚 / 重新启用。

## 已知限制 / 后续方向

- **沙箱不是强隔离**：AST 白名单 + 受限 builtins 能挡住常见误用，但不是安全边界。工具能触及敏感系统时，应把脚本执行放到独立进程 / 容器。
- **单进程**：进化器是进程内 asyncio 队列，文件技能库用进程内锁。多进程 / 多实例部署需要外部队列和带锁的存储（可通过自定义 `SkillStore` 实现）。
- **命名空间（多租户）**：尚未支持，计划后续加入，届时通过 `schema_version` 迁移已有数据。
- **共享技能的上游更新**：技能一旦复制到本地，本地版本优先，不会自动合并上游更新。
- **浏览器**：每个 Agent 共用一个浏览器上下文，暂不支持文件上传、验证码；`run_sync` 每次调用结束都会关闭浏览器。
- **browser_agent 的轨迹是黑盒**：browser-use 内部的每一步不会录进轨迹，所以经过它的部分无法被提炼成确定性脚本（只能提炼它前后的确定性步骤）。
- **MCP 服务**：目前只提供 stdio 传输；HTTP 传输（Streamable HTTP）待做。技能列表变化时不会主动通知客户端，客户端重新列出工具即可看到。

## 路线图（方案二：分层编译）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | SKILL.md 格式适配、指南型技能、安装、显式调用 | ✅ 完成 |
| 2 | 前置检查（guard）、影子运行后自动转正、权限强制检查 | ✅ 完成 |
| 3 | MCP 服务端 / 客户端、接入 browser-use 作为 AI 浏览层、命令行 | ✅ 完成 |
| 4 | L1 原子技能层、按热度升级（指南 → 脚本）、多候选提炼、git 技能仓库 + 签名 | 待做 |
