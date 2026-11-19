"""
工具层

所有"对外部世界产生作用"的能力都必须是工具：
- 自由执行时 LLM 只能通过工具行动 → 产生可回放、可提炼的结构化轨迹
- 技能脚本也只能通过 ctx.tool() 调用工具 → 测试时可以录制/回放、拦截副作用
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TRACE_OUTPUT_LIMIT = 4_000  # 写入轨迹 / 喂给 LLM 的输出截断长度
FIXTURE_OUTPUT_LIMIT = 50_000  # 测试回放 fixture 的最大输出长度


class ToolError(Exception):
    """工具调用失败"""


@dataclass
class Tool:
    """工具定义"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    # 是否有副作用（写文件、发请求修改数据等）。可以是按参数判断的函数
    side_effect: bool | Callable[[dict[str, Any]], bool] = False

    def has_side_effect(self, args: dict[str, Any]) -> bool:
        return self.side_effect(args) if callable(self.side_effect) else bool(self.side_effect)

    def spec(self) -> dict[str, Any]:
        """OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    async def invoke(self, args: dict[str, Any]) -> Any:
        res = self.func(**args)
        if inspect.isawaitable(res):
            res = await res
        return res


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
    side_effect: bool | Callable[[dict[str, Any]], bool] = False,
) -> Callable[[Callable[..., Any]], Tool]:
    """把函数声明为工具"""

    def deco(fn: Callable[..., Any]) -> Tool:
        return Tool(
            name=name or fn.__name__,
            description=description or inspect.getdoc(fn) or "",
            parameters=parameters or {"type": "object", "properties": {}},
            func=fn,
            side_effect=side_effect,
        )

    return deco


class ToolRegistry:
    """工具注册表"""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, t: Tool) -> None:
        self._tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def describe(self) -> str:
        """给提炼/修补 prompt 用的工具说明"""
        lines = []
        for t in self._tools.values():
            props = t.parameters.get("properties", {})
            args = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in props.items())
            lines.append(f"- {t.name}({args}): {t.description}")
        return "\n".join(lines)


def to_jsonable(obj: Any) -> Any:
    """转为可 JSON 序列化的对象"""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(obj)


def truncate(obj: Any, limit: int = TRACE_OUTPUT_LIMIT) -> Any:
    """截断过长的输出，保持结构尽量可读"""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return obj
    return text[:limit] + f"...[截断，共 {len(text)} 字符]"


def args_key(name: str, args: dict[str, Any]) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    output: Any = None
    error: str | None = None
    side_effect: bool = False
    ms: int = 0
    step: str | None = None
    replayed: bool = False

    def to_trace(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "output": truncate(self.output),
            "error": self.error,
            "side_effect": self.side_effect,
            "ms": self.ms,
            "step": self.step,
        }

    def to_fixture(self) -> dict[str, Any] | None:
        """用于测试回放；输出太大则不录制（回放时走真实调用）"""
        size = len(json.dumps(self.output, ensure_ascii=False, default=str))
        if size > FIXTURE_OUTPUT_LIMIT:
            return None
        return {"tool": self.tool, "args": self.args, "ok": self.ok, "output": self.output, "error": self.error}


@dataclass
class Toolbox:
    """工具调用入口：执行 + 录制"""

    registry: ToolRegistry
    timeout: float = 60.0
    calls: list[ToolCallRecord] = field(default_factory=list)

    async def call(self, name: str, args: dict[str, Any], step: str | None = None) -> Any:
        t = self.registry.get(name)
        if t is None:
            raise ToolError(f"未知工具: {name}（可用: {', '.join(self.registry.names())}）")
        return await self._invoke(t, args, step)

    async def _invoke(self, t: Tool, args: dict[str, Any], step: str | None) -> Any:
        rec = ToolCallRecord(tool=t.name, args=to_jsonable(args), ok=True, side_effect=t.has_side_effect(args), step=step)
        t0 = time.perf_counter()
        try:
            out = await asyncio.wait_for(t.invoke(args), timeout=self.timeout)
            rec.output = to_jsonable(out)
            return out
        except asyncio.TimeoutError:
            rec.ok, rec.error = False, f"工具 {t.name} 超时（{self.timeout}s）"
            raise ToolError(rec.error) from None
        except ToolError as e:
            rec.ok, rec.error = False, str(e)
            raise
        except TypeError as e:
            rec.ok, rec.error = False, f"参数错误: {e}"
            raise ToolError(rec.error) from e
        except Exception as e:  # noqa: BLE001
            rec.ok, rec.error = False, f"{type(e).__name__}: {e}"
            raise ToolError(rec.error) from e
        finally:
            rec.ms = int((time.perf_counter() - t0) * 1000)
            self.calls.append(rec)

    def side_effect_calls(self) -> list[dict[str, Any]]:
        return [c.to_trace() for c in self.calls if c.side_effect and c.ok]


class ReplayToolbox(Toolbox):
    """
    测试回放用：
    1. 参数完全一致的调用 → 返回录制结果（确定性、零成本）
    2. 有副作用的调用 → 不真正执行，返回 dry_run 标记
    3. 其他只读调用 → 真实执行
    """

    def __init__(self, registry: ToolRegistry, fixtures: list[dict[str, Any]], timeout: float = 60.0) -> None:
        super().__init__(registry=registry, timeout=timeout)
        self._fixtures = {args_key(f["tool"], f["args"]): f for f in fixtures}

    async def call(self, name: str, args: dict[str, Any], step: str | None = None) -> Any:
        t = self.registry.get(name)
        if t is None:
            raise ToolError(f"未知工具: {name}")
        fx = self._fixtures.get(args_key(name, to_jsonable(args)))
        if fx is not None:
            self.calls.append(
                ToolCallRecord(tool=name, args=args, ok=fx["ok"], output=fx.get("output"), error=fx.get("error"), step=step, replayed=True)
            )
            if not fx["ok"]:
                raise ToolError(fx.get("error") or "录制的调用失败")
            return fx.get("output")
        if t.has_side_effect(args):
            self.calls.append(ToolCallRecord(tool=name, args=args, ok=True, output={"dry_run": True}, side_effect=True, step=step, replayed=True))
            return {"ok": True, "dry_run": True}
        return await self._invoke(t, args, step)
