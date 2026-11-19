"""
工具层

所有"对外部世界产生作用"的能力都必须是工具：
- 自由执行时 LLM 只能通过工具行动 → 产生可回放、可提炼的结构化轨迹
- 技能脚本也只能通过 ctx.tool() 调用工具 → 测试时可以录制/回放、拦截副作用

工具的三个安全属性：
- side_effect：有副作用（写入、提交）。测试回放时不真正执行
- confirm：执行前必须经用户确认。由框架在 Toolbox 中强制执行，不依赖脚本或模型自觉
- interactive：需要等用户输入。不受超时限制，回放时使用录制的回答
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


class UserAbort(Exception):
    """用户拒绝确认 / 取消输入：本次运行终止，不算脚本失败，不触发接管与修补"""


@dataclass
class Tool:
    """工具定义"""

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    # 是否有副作用（写文件、发请求修改数据等）。可以是按参数判断的函数
    side_effect: bool | Callable[[dict[str, Any]], bool] = False
    # 执行前是否必须经用户确认（提交表单、付款、发送等）
    confirm: bool | Callable[[dict[str, Any]], bool] = False
    # 确认时展示给用户的说明
    confirm_message: Callable[[dict[str, Any]], str] | None = None
    # 交互式工具（需要等用户输入）
    interactive: bool = False

    def has_side_effect(self, args: dict[str, Any]) -> bool:
        return self.side_effect(args) if callable(self.side_effect) else bool(self.side_effect)

    def needs_confirm(self, args: dict[str, Any]) -> bool:
        return self.confirm(args) if callable(self.confirm) else bool(self.confirm)

    def describe_call(self, args: dict[str, Any]) -> str:
        if self.confirm_message:
            return self.confirm_message(args)
        return f"即将执行 {self.name}（{json.dumps(args, ensure_ascii=False, default=str)[:300]}）"

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
    confirm: bool | Callable[[dict[str, Any]], bool] = False,
    confirm_message: Callable[[dict[str, Any]], str] | None = None,
    interactive: bool = False,
) -> Callable[[Callable[..., Any]], Tool]:
    """把函数声明为工具"""

    def deco(fn: Callable[..., Any]) -> Tool:
        return Tool(
            name=name or fn.__name__,
            description=description or inspect.getdoc(fn) or "",
            parameters=parameters or {"type": "object", "properties": {}},
            func=fn,
            side_effect=side_effect,
            confirm=confirm,
            confirm_message=confirm_message,
            interactive=interactive,
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
            flags = [x for x, on in (("需用户确认", bool(t.confirm)), ("有副作用", bool(t.side_effect))) if on]
            lines.append(f"- {t.name}({args}): {t.description}" + (f"【{'，'.join(flags)}】" if flags else ""))
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


class _Waiting:
    """标记"正在等用户"，这段时间不计入步骤超时"""

    def __init__(self, box: "Toolbox") -> None:
        self.box = box

    async def __aenter__(self) -> None:
        self.box.waiting_since = time.perf_counter()

    async def __aexit__(self, *exc: Any) -> None:
        if self.box.waiting_since is not None:
            self.box.wait_seconds += time.perf_counter() - self.box.waiting_since
        self.box.waiting_since = None


@dataclass
class Toolbox:
    """工具调用入口：确认 → 执行 → 录制"""

    registry: ToolRegistry
    timeout: float = 60.0
    asker: Any = None  # no_agent.interact.Asker；None 表示无法询问用户
    auto_confirm: bool = False
    calls: list[ToolCallRecord] = field(default_factory=list)
    wait_seconds: float = 0.0  # 累计等待用户的时间
    waiting_since: float | None = None

    async def call(self, name: str, args: dict[str, Any], step: str | None = None) -> Any:
        t = self.registry.get(name)
        if t is None:
            raise ToolError(f"未知工具: {name}（可用: {', '.join(self.registry.names())}）")
        if t.needs_confirm(args) and not self.auto_confirm:
            await self._confirm(t, args)
        return await self._invoke(t, args, step)

    async def _confirm(self, t: Tool, args: dict[str, Any]) -> None:
        if self.asker is None:
            raise UserAbort(f"{t.name} 需要用户确认，但当前没有可用的询问方式（可开启 auto_confirm）")
        async with self.waiting():
            ok = await self.asker.confirm(t.describe_call(args))
        if not ok:
            raise UserAbort(f"用户取消了 {t.name}")

    def waiting(self) -> _Waiting:
        return _Waiting(self)

    def waited_total(self) -> float:
        """累计等待用户的时间（含正在进行的等待）"""
        ongoing = time.perf_counter() - self.waiting_since if self.waiting_since is not None else 0.0
        return self.wait_seconds + ongoing

    async def _invoke(self, t: Tool, args: dict[str, Any], step: str | None) -> Any:
        rec = ToolCallRecord(tool=t.name, args=to_jsonable(args), ok=True, side_effect=t.has_side_effect(args), step=step)
        t0 = time.perf_counter()
        try:
            if t.interactive:
                async with self.waiting():
                    out = await t.invoke(args)
            else:
                out = await asyncio.wait_for(t.invoke(args), timeout=self.timeout)
            rec.output = to_jsonable(out)
            return out
        except UserAbort as e:
            rec.ok, rec.error = False, f"用户取消: {e}"
            raise
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
    1. 参数完全一致的调用 → 返回录制结果（确定、零成本）
    2. 交互式工具（问用户）→ 参数不一致时按顺序回放录制的回答，绝不真的去问
    3. 有副作用 / 需确认的调用 → 不真正执行，返回 dry_run 标记
    4. 其他只读调用 → 真实执行
    """

    def __init__(self, registry: ToolRegistry, fixtures: list[dict[str, Any]], timeout: float = 60.0) -> None:
        super().__init__(registry=registry, timeout=timeout)
        self._fixtures = {args_key(f["tool"], f["args"]): f for f in fixtures}
        self._answers: dict[str, list[dict[str, Any]]] = {}
        for f in fixtures:
            t = registry.get(f["tool"])
            if t is not None and t.interactive:
                self._answers.setdefault(f["tool"], []).append(f)

    def _replay(self, name: str, args: dict[str, Any], fx: dict[str, Any], step: str | None) -> Any:
        self.calls.append(
            ToolCallRecord(tool=name, args=args, ok=fx["ok"], output=fx.get("output"), error=fx.get("error"), step=step, replayed=True)
        )
        if not fx["ok"]:
            raise ToolError(fx.get("error") or "录制的调用失败")
        return fx.get("output")

    async def call(self, name: str, args: dict[str, Any], step: str | None = None) -> Any:
        t = self.registry.get(name)
        if t is None:
            raise ToolError(f"未知工具: {name}")
        fx = self._fixtures.get(args_key(name, to_jsonable(args)))
        if t.interactive:
            queue = self._answers.get(name, [])
            if fx is None:
                if not queue:
                    raise ToolError(f"测试回放中没有录制的 {name} 回答")
                fx = queue[0]
            if fx in queue:
                queue.remove(fx)
            return self._replay(name, args, fx, step)
        if fx is not None:
            return self._replay(name, args, fx, step)
        if t.has_side_effect(args) or t.needs_confirm(args):
            self.calls.append(
                ToolCallRecord(tool=name, args=args, ok=True, output={"dry_run": True}, side_effect=True, step=step, replayed=True)
            )
            return {"ok": True, "dry_run": True}
        return await self._invoke(t, args, step)
