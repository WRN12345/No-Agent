"""
工具层

所有"对外部世界产生作用"的能力都必须是工具：
- 自由执行时 LLM 只能通过工具行动 → 产生可回放、可提炼的结构化轨迹
- 技能脚本也只能通过 ctx.tool() 调用工具 → 测试时可以录制/回放、拦截副作用

工具的三个安全属性：
- side_effect：有副作用（写入、提交）。测试回放时不真正执行
- confirm：执行前必须经用户确认。由框架在 Toolbox 中强制执行，不依赖脚本或模型自觉
- interactive：需要等用户输入。不受超时限制，回放时使用录制的回答
- stateful：结果依赖实时环境状态（如浏览器当前页面）。回放时只用录制结果，没有录制就报错，绝不真实执行——
  否则后台的测试 / 影子运行会去操作用户正在使用的浏览器，结果也不可复现
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

TRACE_OUTPUT_LIMIT = 4_000  # 写入轨迹 / 喂给 LLM 的输出截断长度
FIXTURE_OUTPUT_LIMIT = 50_000  # 测试回放 fixture 的最大输出长度
STATEFUL_FIXTURE_LIMIT = 1_000_000  # stateful 工具回放时不能真实执行，录制上限放宽


class ToolError(Exception):
    """工具调用失败"""


class PermissionDenied(ToolError):
    """技能调用了未声明的工具 / 访问了未声明的域名"""


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
    # 结果依赖实时环境状态（浏览器页面等）：回放时只用录制结果，不真实执行
    stateful: bool = False
    # 哪个参数是要访问的 URL（用于技能的域名权限检查）
    url_arg: str | None = None

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
    stateful: bool = False,
    url_arg: str | None = None,
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
            stateful=stateful,
            url_arg=url_arg,
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


ALWAYS_ALLOWED = {"ask_user"}  # 不需要声明的工具

# 子技能调用期间生效的权限：{toolbox id: (工具集合 | None, 域名列表 | None)}。
# 用 ContextVar 保存而不是改 toolbox 的属性：并发的 ctx.skill 调用（各自在独立的 asyncio 任务中）互不影响。
_DELEGATED: contextvars.ContextVar[dict[int, tuple[set[str] | None, list[str] | None]]] = contextvars.ContextVar(
    "no_agent_delegated_permissions", default={}
)


def host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def domain_allowed(host: str, domains: list[str]) -> bool:
    """example.com 匹配自身和所有子域名；*.example.com 只匹配子域名"""
    for d in domains:
        d = d.lower().strip()
        if d.startswith("*."):
            if host.endswith(d[1:]):
                return True
        elif host == d or host.endswith("." + d):
            return True
    return False


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
    stateful: bool = False

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
        if size > (STATEFUL_FIXTURE_LIMIT if self.stateful else FIXTURE_OUTPUT_LIMIT):
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
    # 当前生效的技能权限（None = 不限制；执行技能脚本时设置，AI 接管前清除）
    allowed_tools: set[str] | None = None
    allowed_domains: list[str] | None = None

    def restrict(self, tools: list[str] | None, domains: list[str] | None) -> None:
        self.allowed_tools = set(tools) | ALWAYS_ALLOWED if tools else None
        self.allowed_domains = list(domains) if domains else None

    def effective_permissions(self) -> tuple[set[str] | None, list[str] | None]:
        """当前生效的 (工具, 域名) 限制；None 表示不限制"""
        return _DELEGATED.get().get(id(self), (self.allowed_tools, self.allowed_domains))

    @property
    def tools_restricted(self) -> bool:
        """当前是否限制了可用工具（此时调用子技能也必须事先声明）"""
        return self.effective_permissions()[0] is not None

    @contextmanager
    def delegated(self, tools: list[str] | None, domains: list[str] | None) -> Iterator[None]:
        """
        子技能调用：子技能在自己声明的权限下运行（能力委托——调用方必须事先声明可调用它）。
        子技能没有声明的部分（工具或域名）不会因此放开，而是继承调用方当前的限制。
        """
        cur_tools, cur_domains = self.effective_permissions()
        eff = (
            set(tools) | ALWAYS_ALLOWED if tools else cur_tools,
            list(domains) if domains else cur_domains,
        )
        token = _DELEGATED.set({**_DELEGATED.get(), id(self): eff})
        try:
            yield
        finally:
            _DELEGATED.reset(token)

    def check_permission(self, t: Tool, args: dict[str, Any]) -> None:
        tools, domains = self.effective_permissions()
        if tools is not None and t.name not in tools:
            raise PermissionDenied(f"技能未声明使用工具 {t.name}（已声明: {', '.join(sorted(tools))}）")
        if domains is not None and t.url_arg and args.get(t.url_arg):
            host = host_of(str(args[t.url_arg]))
            if not domain_allowed(host, domains):
                raise PermissionDenied(f"技能未声明访问域名 {host or args[t.url_arg]}（已声明: {', '.join(domains)}）")

    async def call(self, name: str, args: dict[str, Any], step: str | None = None) -> Any:
        t = self.registry.get(name)
        if t is None:
            raise ToolError(f"未知工具: {name}（可用: {', '.join(self.registry.names())}）")
        self.check_permission(t, args)
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
        rec = ToolCallRecord(
            tool=t.name, args=to_jsonable(args), ok=True, side_effect=t.has_side_effect(args), step=step, stateful=t.stateful
        )
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
    4. 依赖实时状态的调用（stateful）→ 没有录制就报错，不真实执行
    5. 其他只读调用 → 真实执行
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
        self.check_permission(t, args)
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
        if t.stateful:
            raise ToolError(f"回放中没有录制 {name} 的这次调用（{name} 依赖实时状态，回放时不真实执行）")
        return await self._invoke(t, args, step)
