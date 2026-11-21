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
pip install No-Agent                 # 核心库
pip install "No-Agent[server]"       # HTTP 服务（FastAPI）
pip install "No-Agent[browser]"      # 浏览器工具（Playwright，默认使用本机 Chrome）
pip install "No-Agent[mcp]"          # MCP 服务端 / 客户端
pip install "No-Agent[browser-use]"  # browser-use 浏览器 Agent（开放式网页探索）
pip install "No-Agent[sign]"         # 共享技能签名
```

发布到 PyPI 之前，直接从 GitHub 安装：

```bash
pip install "git+https://github.com/WRN12345/No-Agent.git"
pip install "No-Agent[browser,mcp] @ git+https://github.com/WRN12345/No-Agent.git"   # 带可选依赖
```

> 命名约定：发行包和命令行叫 `No-Agent`；代码里 `import no_agent`；环境变量前缀 `NO_AGENT_`，数据目录 `.no-agent/`。

## 快速开始

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
    print(run.mode, run.result)                    # mode: skill / skill_takeover / guide / freeform / aborted / error

run = agent.run_sync("查一下所有待发货的订单")      # 同步代码中使用
agent.feedback(run.id, -1, "漏了今天的订单")         # 可选：反馈结果；脚本"成功"但结果不对时触发修补

# 技能沉淀出来之后（No-Agent skills 可查看），已知用哪个技能时可以显式调用，跳过匹配
run = await agent.skill("query-orders").run({"status": "待发货"})
```

- **配置**：也可以从环境变量读取：`Agent(AgentConfig.from_env())`，变量见 [.env.example](.env.example)。环境变量只在 `from_env()` 里生效（CLI、HTTP 服务和 Docker 镜像用的就是它）；直接构造 `AgentConfig(...)` 时不读环境变量（`NO_AGENT_HOME` 除外）。全部配置项见 [config.py](src/no_agent/config.py)。
- **工具**：对外部世界的一切操作都必须是工具。有副作用的工具标注 `side_effect=True`，需要用户确认的标注 `confirm=True`，结果依赖实时环境（如浏览器页面）的标注 `stateful=True`——后台的回放测试依赖这些标注，见 [录制回放](docs/evolution.md#录制回放)。

## 核心概念

- **技能**：一个 SKILL.md 目录（兼容 Anthropic Agent Skills），带流程脚本、参数 schema、回归用例和版本历史。流程型直接执行脚本，指南型由 AI 照着做。→ [技能](docs/skills.md)
- **接管与修补**：脚本在第 k 步失败，AI 从第 k 步接着完成；后台只重写出错的那个步骤函数。→ [执行与进化](docs/evolution.md)
- **提炼与升级**：同类任务 AI 做多了提炼成新技能；指南型技能用多了升级成脚本。→ [技能怎么长出来](docs/evolution.md#技能怎么长出来)
- **上线门禁**：回归测试 → 变更审查（扩大能力时等人批准）→ 影子运行（与真实结果比对 N 次一致才转正）。→ [上线门禁](docs/evolution.md#上线门禁)
- **安全**：脚本在一次性子进程中执行（Linux 上可加内核约束），按声明的权限运行，共享技能按签名决定信任。→ [安全模型](docs/security.md)

## 文档

| 文档 | 内容 |
|---|---|
| [技能](docs/skills.md) | 目录格式、流程型与指南型、安装与挂载、显式调用、脚本怎么写、技能组合与原子技能 |
| [执行与进化](docs/evolution.md) | 一次运行的流程与各 mode、后台进化任务、上线门禁、录制回放、下线与回滚 |
| [安全模型](docs/security.md) | 脚本隔离档位、权限、变更审查、共享技能的签名与信任 |
| [浏览器与人机交互](docs/browser.md) | 浏览器工具、内置 fill-form 技能、browser-use、提问与确认 |
| [MCP](docs/mcp.md) | 作为 MCP 服务被其他 Agent 调用；作为客户端接入外部 MCP 工具 |
| [服务与命令行](docs/server.md) | HTTP 服务、鉴权、接口列表、Docker、`No-Agent` 命令 |
| [数据存储](docs/storage.md) | `.no-agent/` 目录、位置解析、分层与写时复制、自定义存储、结构迁移 |
| [架构](docs/architecture.md) | 设计取舍、代码结构、已知限制、路线图 |

## 开发

```bash
pip install -e ".[dev]"
python -m pytest                                      # 离线跑通完整进化闭环（假 LLM）
NO_AGENT_SCRIPT_ISOLATION=sandbox python -m pytest    # Linux 上用 sandbox 档跑整套测试
```
