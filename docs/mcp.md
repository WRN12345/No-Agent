# MCP：被其他 Agent 调用，也调用别人的工具

需要 `pip install "No-Agent[mcp]"`。

## 作为 MCP 服务

Claude Desktop / Claude Code / Cursor 等都能接入：

```json
{"mcpServers": {"No-Agent": {"command": "No-Agent", "args": ["mcp"], "env": {"NO_AGENT_HOME": "/path/to/project/.no-agent"}}}}
```

| 暴露内容 | 说明 |
|---|---|
| `run_task(task)` | 执行任意任务（自动选择 技能脚本 / AI 接管 / AI 自由执行） |
| `list_skills()` | 列出技能 |
| `feedback(run_id, score, comment)` | 反馈运行结果（-1 ~ 1）；脚本"成功"但结果不对时给负分，触发后台修补 |
| `skill_<id>(...)` | 每个流程型技能一个工具，参数就是技能的 `params_schema`（显式调用，零匹配开销）；按技能权限标注 `readOnlyHint` / `destructiveHint` |
| 资源 `skill://<id>/SKILL.md` | 每个技能的 SKILL.md，客户端可以直接读来当指南 |

技能里的 `ctx.ask()` 和需要确认的操作，会变成 **MCP elicitation**，由客户端弹表单问用户。客户端拒绝或不支持 elicitation 时，运行以 `aborted` 结束，绝不会跳过确认。

代码里使用：`create_mcp_server(agent)` / `await serve_stdio(agent)`（[integrations/mcp_server.py](../src/no_agent/integrations/mcp_server.py)）。

限制：目前只提供 stdio 传输，HTTP 传输（Streamable HTTP）待做；技能列表变化时不会主动通知客户端，客户端重新列出工具即可看到。

## 作为 MCP 客户端

外部 MCP 服务的工具注册为本地工具（`<name>__<工具名>`），和内置工具一样被录制、回放、提炼进技能（[integrations/mcp_client.py](../src/no_agent/integrations/mcp_client.py)）：

```python
from no_agent.config import McpServerConfig

AgentConfig(mcp_servers=[
    McpServerConfig(name="github", command="npx", args=["-y", "@modelcontextprotocol/server-github"]),
    McpServerConfig(name="crm", url="https://crm.example.com/mcp", headers={"Authorization": "Bearer ..."}),
])
```

MCP 工具注解的映射：`readOnlyHint=true` → 无副作用；其他 → 有副作用（回放测试时不执行）；`destructiveHint=true` → 执行前请求确认。
