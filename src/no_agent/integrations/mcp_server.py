"""
MCP 服务端：把本框架暴露给其他 Agent（Claude Code / Claude Desktop / Cursor / 任意 MCP 客户端）

工具：
  run_task(task, mode)       执行任意任务（自动选择 技能脚本 / AI 接管 / AI 自由执行）
  list_skills()              列出技能
  skill_<id>(参数...)        每个流程型技能一个工具，参数即技能的 params_schema（显式调用，跳过匹配）
资源：
  skill://<id>/SKILL.md      每个技能的 SKILL.md，客户端可以直接读取作为指南
交互：
  技能里的 ctx.ask / 需要确认的操作 → MCP elicitation，由客户端向用户展示表单
  客户端不支持 elicitation 时：提问失败、需确认的操作被拒绝（mode=aborted）

启动（stdio，供 Claude Desktop / Claude Code 等配置使用）：
    no-agent mcp
或在代码里：
    await serve_stdio(agent)

需要 pip install "No-Agent[mcp]"。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from no_agent.interact import Question
from no_agent.tools.base import UserAbort

if TYPE_CHECKING:
    from no_agent.agent import Agent

logger = logging.getLogger(__name__)

SKILL_TOOL_PREFIX = "skill_"
SKILL_URI = "skill://{id}/SKILL.md"


class McpAsker:
    """通过 MCP elicitation 向客户端的用户提问 / 请求确认"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def _session(self) -> Any:
        try:
            return self.server.request_context.session
        except LookupError as e:
            raise UserAbort("当前不在 MCP 请求中，无法询问用户") from e

    async def _elicit(self, message: str, schema: dict[str, Any]) -> dict[str, Any]:
        import mcp.types as types

        session = self._session()
        if not session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())):
            raise UserAbort("MCP 客户端不支持 elicitation，无法询问用户")
        result = await session.elicit(message=message, requestedSchema=schema)
        if result.action != "accept":
            raise UserAbort(f"用户{'拒绝' if result.action == 'decline' else '取消'}了")
        return dict(result.content or {})

    async def ask(self, questions: list[Question], *, title: str = "") -> dict[str, Any]:
        # elicitation 的表单字段名用 q0、q1……，避免选择器之类的特殊字符
        props: dict[str, Any] = {}
        required = []
        for i, q in enumerate(questions):
            key = f"q{i}"
            if q.kind == "confirm":
                prop: dict[str, Any] = {"type": "boolean"}
            elif q.kind == "choice" and q.options:
                prop = {"type": "string", "enum": q.options}
            else:
                prop = {"type": "string"}
            prop["title"] = q.label
            if q.help:
                prop["description"] = q.help
            if q.default not in (None, ""):
                prop["default"] = q.default
            props[key] = prop
            if q.required:
                required.append(key)
        content = await self._elicit(title or "需要你提供以下信息", {"type": "object", "properties": props, "required": required})
        return {q.key: content[f"q{i}"] for i, q in enumerate(questions) if f"q{i}" in content}

    async def confirm(self, message: str) -> bool:
        schema = {
            "type": "object",
            "properties": {"confirm": {"type": "boolean", "title": "确认执行"}},
            "required": ["confirm"],
        }
        try:
            content = await self._elicit(message, schema)
        except UserAbort:
            return False
        return bool(content.get("confirm"))


def _run_summary(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "mode": run.mode,
        "success": run.success,
        "result": run.result,
        "error": run.error,
        "skill_id": run.skill_id,
        "skill_version": run.skill_version,
        "tokens": run.prompt_tokens + run.completion_tokens,
    }


def create_mcp_server(agent: Agent, *, name: str = "no-agent", expose_skills: bool = True) -> Any:
    """创建 MCP Server（低层 API，技能列表变化时工具列表随之变化）"""
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError as e:
        raise ImportError('MCP 服务需要额外依赖：pip install "No-Agent[mcp]"') from e

    from no_agent import __version__

    server: Any = Server(
        name,
        version=__version__,
        instructions=(
            "自进化 Agent：能用技能脚本就用脚本（快、省、确定），否则由 AI 完成，并把经验沉淀成技能。"
            "已知要用哪个技能时，直接调用 skill_<id> 工具；否则用 run_task。"
        ),
    )
    agent.set_asker(McpAsker(server))

    def skill_tools() -> dict[str, Any]:
        return {f"{SKILL_TOOL_PREFIX}{s.id}": s for s in agent.skills() if s.kind == "flow"} if expose_skills else {}

    def annotations_for(skill: Any) -> Any:
        declared = skill.permissions.tools
        tools = [agent.registry.get(t) for t in declared]
        known = declared and all(t is not None for t in tools)
        read_only = bool(known) and not any(t.side_effect or t.confirm for t in tools if t)
        destructive = bool(known) and any(t.confirm for t in tools if t)
        return types.ToolAnnotations(title=skill.name, readOnlyHint=read_only, destructiveHint=destructive if known else None)

    @server.list_tools()
    async def list_tools() -> list[Any]:
        tools = [
            types.Tool(
                name="run_task",
                description="执行一个任务：命中已有技能就直接跑脚本，否则由 AI 完成（成功后会沉淀为技能）。返回执行方式、结果、token 消耗。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "任务描述"},
                        "mode": {"type": "string", "enum": ["auto", "freeform"], "default": "auto"},
                    },
                    "required": ["task"],
                },
            ),
            types.Tool(
                name="list_skills",
                description="列出可用的技能（id、类型、描述、版本、成功率）",
                inputSchema={"type": "object", "properties": {}},
                annotations=types.ToolAnnotations(readOnlyHint=True),
            ),
        ]
        for tool_name, s in skill_tools().items():
            desc = s.description + (f"\n适用：{s.when_to_use}" if s.when_to_use else "")
            schema = dict(s.params_schema or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            tools.append(types.Tool(name=tool_name, description=desc, inputSchema=schema, annotations=annotations_for(s)))
        return tools

    @server.call_tool(validate_input=False)
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> list[Any]:
        arguments = arguments or {}
        if tool_name == "run_task":
            run = await agent.run(str(arguments.get("task", "")), mode=arguments.get("mode", "auto"))
            data: Any = _run_summary(run)
        elif tool_name == "list_skills":
            data = [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "description": s.description,
                    "version": s.active_version,
                    "runs": s.stats.runs,
                    "success_rate": round(s.stats.successes / s.stats.runs, 3) if s.stats.runs else None,
                }
                for s in agent.skills()
            ]
        elif tool_name in (st := skill_tools()):
            try:
                run = await agent.skill(st[tool_name].id).run(arguments)
            except ValueError as e:
                raise ValueError(str(e)) from e
            data = _run_summary(run)
        else:
            raise ValueError(f"未知工具: {tool_name}")
        return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]

    @server.list_resources()
    async def list_resources() -> list[Any]:
        return [
            types.Resource(
                uri=SKILL_URI.format(id=s.id),
                name=s.id,
                title=s.name,
                description=s.description,
                mimeType="text/markdown",
            )
            for s in agent.skills()
        ]

    @server.read_resource()
    async def read_resource(uri: Any) -> str:
        u = str(uri)
        prefix, suffix = "skill://", "/SKILL.md"
        if not (u.startswith(prefix) and u.endswith(suffix)):
            raise ValueError(f"未知资源: {u}")
        sid = u[len(prefix) : -len(suffix)]
        if agent.store.get(sid) is None:
            raise ValueError(f"技能不存在: {sid}")
        return (agent.store.path(sid) / "SKILL.md").read_text(encoding="utf-8")

    return server


async def serve_stdio(agent: Agent, *, name: str = "no-agent") -> None:
    """以 stdio 方式运行 MCP 服务（直到客户端断开）"""
    from mcp.server.stdio import stdio_server

    server = create_mcp_server(agent, name=name)
    async with agent:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
