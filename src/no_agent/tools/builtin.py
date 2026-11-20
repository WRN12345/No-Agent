"""
内置通用工具

业务方按同样方式用 @tool 定义自己的工具，再 registry.register() 注册即可。
注意正确标注 side_effect：测试回放时有副作用的调用不会真正执行。
"""

from __future__ import annotations

import ast
import operator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from no_agent.tools.base import Tool, ToolError, tool

# ── calculator ──

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 1000:
            raise ToolError("指数过大")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("只支持数字和 + - * / // % ** 运算")


@tool(
    "calculator",
    description="计算数学表达式，例如 '(3 + 5) * 2 ** 3'",
    parameters={
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "数学表达式"}},
        "required": ["expression"],
    },
)
def calculator(expression: str) -> float:
    try:
        return _eval(ast.parse(expression, mode="eval"))
    except SyntaxError as e:
        raise ToolError(f"表达式语法错误: {e.msg}") from e
    except ZeroDivisionError as e:
        raise ToolError("除数为零") from e


# ── http ──

_MAX_BODY = 100_000


@tool(
    "http_request",
    url_arg="url",
    description="发送 HTTP 请求，返回 {status, content_type, body}。body 超过 100k 字符会截断。",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string", "default": "GET"},
            "headers": {"type": "object"},
            "params": {"type": "object", "description": "URL 查询参数"},
            "json_body": {"description": "JSON 请求体"},
            "data": {"type": "string", "description": "原始请求体"},
        },
        "required": ["url"],
    },
    side_effect=lambda a: str(a.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"},
)
async def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: str | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.request(method.upper(), url, headers=headers, params=params, json=json_body, content=data)
    body = resp.text
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "body": body[:_MAX_BODY],
        "truncated": len(body) > _MAX_BODY,
    }


# ── workspace 文件 ──


def make_file_tools(workspace: Path) -> list[Tool]:
    """文件工具，限制在 workspace 目录内"""
    root = workspace.resolve()

    def _resolve(path: str) -> Path:
        p = (root / path).resolve()
        if p != root and root not in p.parents:
            raise ToolError("路径超出工作区")
        return p

    @tool(
        "read_file",
        description="读取工作区内的文本文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    def read_file(path: str) -> str:
        p = _resolve(path)
        if not p.is_file():
            raise ToolError(f"文件不存在: {path}")
        return p.read_text(encoding="utf-8", errors="replace")

    @tool(
        "write_file",
        description="写入工作区内的文本文件（覆盖），自动创建目录",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        side_effect=True,
    )
    def write_file(path: str, content: str) -> dict[str, Any]:
        p = _resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    @tool(
        "list_files",
        description="列出工作区内某目录下的文件",
        parameters={"type": "object", "properties": {"path": {"type": "string", "default": "."}}},
    )
    def list_files(path: str = ".") -> list[str]:
        p = _resolve(path)
        if not p.is_dir():
            raise ToolError(f"目录不存在: {path}")
        return sorted(str(c.relative_to(root)).replace("\\", "/") + ("/" if c.is_dir() else "") for c in p.iterdir())

    return [read_file, write_file, list_files]


@tool("current_time", description="获取当前本地时间（ISO 格式）")
def current_time() -> str:
    return datetime.now().isoformat(timespec="seconds")


def builtin_tools(workspace: Path) -> list[Tool]:
    """内置通用工具；文件工具限制在 workspace 内"""
    workspace.mkdir(parents=True, exist_ok=True)
    return [calculator, http_request, current_time, *make_file_tools(workspace)]
