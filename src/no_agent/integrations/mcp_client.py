"""
MCP 客户端：把外部 MCP 服务的工具接入为本地工具

    AgentConfig(mcp_servers=[
        McpServerConfig(name="github", command="npx", args=["-y", "@modelcontextprotocol/server-github"]),
        McpServerConfig(name="crm", url="https://crm.example.com/mcp", headers={"Authorization": "Bearer ..."}),
    ])

工具以 "<name>__<工具名>" 注册。MCP 的工具注解映射为本框架的安全属性：
  readOnlyHint=true      → 无副作用（测试回放时可真实调用）
  其他                   → 视为有副作用（测试回放时不执行）
  destructiveHint=true   → 执行前请求用户确认（可用 confirm_destructive=False 关闭）
接入后，这些工具和内置工具一样会被录制、回放、提炼进技能脚本。

需要 pip install "No-Agent[mcp]"。
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from no_agent.tools.base import Tool, ToolError

if TYPE_CHECKING:
    from no_agent.config import McpServerConfig

logger = logging.getLogger(__name__)


def _content_to_output(result: Any) -> Any:
    """MCP CallToolResult → 普通 Python 值"""
    if getattr(result, "structuredContent", None) is not None:
        sc = result.structuredContent
        # FastMCP 会把非对象返回值包成 {"result": ...}
        return sc["result"] if isinstance(sc, dict) and list(sc) == ["result"] else sc
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except (json.JSONDecodeError, TypeError):
            return texts[0]
    others = [c.model_dump(exclude_none=True) for c in result.content if getattr(c, "type", None) != "text"]
    return {"text": "\n".join(texts), "content": others} if others else "\n".join(texts)


class McpToolset:
    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg
        self._stack: AsyncExitStack | None = None
        self.session: Any = None

    async def connect(self) -> list[Tool]:
        try:
            from mcp import ClientSession
        except ImportError as e:
            raise ImportError('接入 MCP 服务需要额外依赖：pip install "No-Agent[mcp]"') from e

        if bool(self.cfg.command) == bool(self.cfg.url):
            raise ValueError(f"MCP 服务 {self.cfg.name}：command 和 url 必须二选一")
        stack = AsyncExitStack()
        try:
            if self.cfg.command:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                params = StdioServerParameters(command=self.cfg.command, args=self.cfg.args, env=self.cfg.env, cwd=self.cfg.cwd)
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await stack.enter_async_context(streamablehttp_client(self.cfg.url, headers=self.cfg.headers))
            self.session = await stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            listed = await self.session.list_tools()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return [self._wrap(t) for t in listed.tools if self.cfg.include is None or t.name in self.cfg.include]

    def _wrap(self, mt: Any) -> Tool:
        ann = mt.annotations
        read_only = bool(ann and ann.readOnlyHint)
        destructive = bool(ann and ann.destructiveHint)
        remote_name = mt.name
        session_ref = self

        async def call(**kwargs: Any) -> Any:
            if session_ref.session is None:
                raise ToolError(f"MCP 服务 {session_ref.cfg.name} 未连接")
            result = await session_ref.session.call_tool(remote_name, kwargs)
            output = _content_to_output(result)
            if result.isError:
                raise ToolError(f"{session_ref.cfg.name}.{remote_name} 返回错误: {output}")
            return output

        title = f"[{self.cfg.name}] "
        return Tool(
            name=f"{self.cfg.name}__{mt.name}",
            description=title + (mt.description or mt.name),
            parameters=mt.inputSchema or {"type": "object", "properties": {}},
            func=call,
            side_effect=not read_only,
            confirm=destructive and self.cfg.confirm_destructive,
        )

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 MCP 连接 %s 出错", self.cfg.name, exc_info=True)
            self._stack = None
            self.session = None
