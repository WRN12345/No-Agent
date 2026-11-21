"""
技能脚本沙箱：静态校验、受限加载、脚本侧的 ctx、单个阶段的执行与错误分类

本模块只依赖标准库、不 import no_agent 的任何东西：
进程内执行（InProcessRunner）和子进程 worker（skills/_worker.py）共用同一份代码。
脚本通过 ctx 对外部世界的一切请求都交给 Bridge（宿主侧实现权限检查、工具、LLM、子技能）。

安全说明：这里的 AST 白名单 + 受限 builtins + 模块代理能挡住常见误用和已知的逃逸手法
（import os、经白名单模块转手拿到 os/sys、帧对象回溯、match 类模式读私有属性、
attrgetter / Formatter 等"字符串版 getattr"），但它仍然不是强安全边界：
脚本与框架同进程运行时，死循环之类也无法被打断。不可信来源的脚本不应在进程内执行
（默认的 process 档在子进程中执行，sandbox 档再加内核约束）。
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
import json
import traceback
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

SCRIPT_FILENAME = "<skill>"

ALLOWED_IMPORTS = {
    "json", "re", "math", "datetime", "time", "random", "statistics", "collections", "itertools",
    "functools", "string", "textwrap", "html", "urllib", "base64", "hashlib", "decimal",
    "fractions", "typing", "uuid", "csv", "io", "difflib", "unicodedata", "copy", "enum",
    "operator", "bisect", "heapq", "zoneinfo", "calendar",
}
# 允许的子模块（其他子模块一律不允许，例如 urllib.request 可以绕过 ctx.tool 直接联网 / 读文件）
ALLOWED_SUBMODULES = {"urllib.parse", "collections.abc"}

# 白名单模块中会破坏沙箱的成员：等价于 getattr / eval / open 的能力
DENIED_MEMBERS: dict[str, set[str]] = {
    "operator": {"attrgetter", "methodcaller"},
    "string": {"Formatter"},
    "typing": {"get_type_hints", "evaluate_forward_ref", "ForwardRef"},
    "functools": {"singledispatch", "singledispatchmethod"},
    "io": {"open", "open_code", "FileIO"},
    "zoneinfo": {"reset_tzpath"},
}

# 不以下划线开头、但能拿到帧 / 全局变量（进而拿到真实 builtins）的属性
FORBIDDEN_ATTRS = {
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "ag_await",
    "f_back", "f_globals", "f_locals", "f_builtins", "f_code", "tb_frame", "tb_next",
}

FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "input", "breakpoint", "exit", "quit", "memoryview", "help",
}

_SAFE_BUILTIN_NAMES = [
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hasattr", "hash", "hex", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object",
    "oct", "ord", "pow", "print", "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip", "True", "False", "None",
    "Exception", "ValueError", "KeyError", "TypeError", "RuntimeError", "IndexError",
    "AttributeError", "ZeroDivisionError", "StopIteration", "NotImplementedError", "AssertionError",
    "LookupError", "ArithmeticError", "UnicodeDecodeError",
]


class ScriptError(Exception):
    """脚本不合法"""


@dataclass
class StepDef:
    name: str
    func: Callable[[Any], Any]
    idempotent: bool = True
    kind: str = "code"


@dataclass
class LoadedScript:
    steps: list[StepDef] = field(default_factory=list)
    verify: Callable[..., Any] | None = None
    guard: Callable[..., Any] | None = None


def _is_step_decorator(d: ast.expr) -> bool:
    if isinstance(d, ast.Name):
        return d.id == "step"
    return isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "step"


def _module_allowed(name: str) -> bool:
    return name in ALLOWED_SUBMODULES if "." in name else name in ALLOWED_IMPORTS


def validate_script(source: str) -> list[str]:
    """静态校验，返回步骤名列表；不合法抛 ScriptError"""
    try:
        tree = ast.parse(source, filename=SCRIPT_FILENAME)
    except SyntaxError as e:
        raise ScriptError(f"语法错误（第 {e.lineno} 行）: {e.msg}") from e

    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_allowed(alias.name):
                    errors.append(f"第 {node.lineno} 行: 不允许 import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _module_allowed(node.module or ""):
                errors.append(f"第 {node.lineno} 行: 不允许 from {node.module} import")
            for alias in node.names:
                if alias.name == "*":
                    errors.append(f"第 {node.lineno} 行: 不允许 import *")
                elif alias.name.startswith("_") or alias.name in DENIED_MEMBERS.get(node.module or "", ()):
                    errors.append(f"第 {node.lineno} 行: 不允许使用 {node.module}.{alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(f"第 {node.lineno} 行: 不允许使用 {node.id}")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            errors.append(f"第 {node.lineno} 行: 不允许使用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            errors.append(f"第 {node.lineno} 行: 不允许访问私有属性 .{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            errors.append(f"第 {node.lineno} 行: 不允许访问 .{node.attr}")
        elif isinstance(node, ast.MatchClass):
            # case X(__class__=c) 等价于读取属性，同样受私有属性规则约束
            for attr in node.kwd_attrs:
                if attr.startswith("_") or attr in FORBIDDEN_ATTRS:
                    errors.append(f"第 {node.lineno} 行: 不允许在模式匹配中访问 {attr}")
        elif isinstance(node, ast.ClassDef):
            errors.append(f"第 {node.lineno} 行: 不允许定义类")

    steps: list[str] = []
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and i == 0:
            continue  # 模块 docstring
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_step = any(_is_step_decorator(d) for d in node.decorator_list)
            if is_step:
                if not isinstance(node, ast.AsyncFunctionDef):
                    errors.append(f"步骤 {node.name} 必须是 async def")
                if node.name in steps:
                    errors.append(f"步骤名重复: {node.name}")
                steps.append(node.name)
            continue
        errors.append(f"第 {node.lineno} 行: 模块顶层只允许 import、常量和函数定义")

    if not steps:
        errors.append("脚本中没有任何 @step 步骤")
    if errors:
        raise ScriptError("；".join(errors))
    return steps


class _ModuleProxy:
    """
    白名单模块的只读代理：只暴露模块自己的公开成员。
    挡住"白名单模块转手"的逃逸（uuid.os、typing.sys ……）和 DENIED_MEMBERS 中的危险成员。
    """

    __slots__ = ("_mod",)

    def __init__(self, mod: types.ModuleType) -> None:
        object.__setattr__(self, "_mod", mod)

    def __getattr__(self, name: str) -> Any:
        mod = object.__getattribute__(self, "_mod")
        modname = mod.__name__
        if name.startswith("_") or name in DENIED_MEMBERS.get(modname, ()):
            raise AttributeError(f"不允许使用 {modname}.{name}")
        value = getattr(mod, name)
        if isinstance(value, types.ModuleType):
            if value.__name__ in ALLOWED_SUBMODULES and value.__name__ == f"{modname}.{name}":
                return _ModuleProxy(value)
            raise AttributeError(f"不允许经由 {modname} 访问模块 {value.__name__}")
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("模块是只读的")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("模块是只读的")

    def __dir__(self) -> list[str]:
        mod = object.__getattribute__(self, "_mod")
        return [n for n in dir(mod) if not n.startswith("_")]

    def __repr__(self) -> str:
        return f"<module {object.__getattribute__(self, '_mod').__name__!r} (sandboxed)>"


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if level or not _module_allowed(name):
        raise ImportError(f"不允许 import {name}")
    # 返回代理：from x import y / import a.b as c 都经由代理的 __getattr__ 取成员
    return _ModuleProxy(builtins.__import__(name, globals, locals, fromlist, level))


def _safe_builtins() -> dict[str, Any]:
    b = {n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES}
    b["__import__"] = _guarded_import
    return b


def load_script(source: str) -> LoadedScript:
    """校验并加载脚本"""
    validate_script(source)
    loaded = LoadedScript()

    def step(_fn=None, *, idempotent: bool = True, kind: str = "code"):
        def register(fn):
            loaded.steps.append(StepDef(name=fn.__name__, func=fn, idempotent=idempotent, kind=kind))
            return fn

        return register(_fn) if callable(_fn) else register

    namespace: dict[str, Any] = {"__builtins__": _safe_builtins(), "__name__": "skill_script", "step": step}
    try:
        exec(compile(source, SCRIPT_FILENAME, "exec"), namespace)  # noqa: S102 - 已经过 AST 校验
    except Exception as e:  # noqa: BLE001
        raise ScriptError(f"加载失败: {type(e).__name__}: {e}") from e
    verify, guard = namespace.get("verify"), namespace.get("guard")
    loaded.verify = verify if callable(verify) else None
    loaded.guard = guard if callable(guard) else None
    return loaded


# ── 脚本侧的 ctx ──

GUARD_STEP = "<guard>"
VERIFY_STEP = "verify"


class StepFailure(Exception):
    """脚本主动判定失败（ctx.fail），或 Bridge 拒绝了请求"""


class Bridge(Protocol):
    """ctx 背后的宿主能力。进程内：直接调用；子进程：经 RPC 转给宿主"""

    async def tool(self, name: str, args: dict[str, Any]) -> Any: ...
    async def llm(self, instruction: str, input: Any, json: bool) -> Any: ...  # noqa: A002
    async def skill(self, skill_id: str, params: dict[str, Any]) -> Any: ...


class ScriptContext:
    """注入给每个步骤的 ctx（脚本作者看到的 API，见 SCRIPT_API_DOC）"""

    def __init__(self, *, task: str, params: dict[str, Any], bridge: Bridge) -> None:
        self.task = task
        self.params = params
        self.state: dict[str, Any] = {}
        self.result: Any = None
        self.logs: list[str] = []
        self._bridge = bridge
        self._step: str | None = None

    async def tool(self, name: str, **args: Any) -> Any:
        return await self._bridge.tool(name, args)

    async def llm(self, instruction: str, input: Any = None, *, json: bool = False) -> Any:  # noqa: A002
        return await self._bridge.llm(instruction, input, json)

    async def skill(self, skill_id: str, **params: Any) -> Any:
        """调用另一个流程型技能（在它自己的权限下运行），返回它的结果；失败时本步骤失败"""
        return await self._bridge.skill(skill_id, params)

    async def ask(self, questions: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
        """一次性向用户提出多个问题，返回 {key: 回答}"""
        return await self.tool("ask_user", questions=questions, title=title)

    async def confirm(self, message: str) -> bool:
        """向用户确认一件事，返回是否同意"""
        out = await self.ask([{"key": "ok", "label": message, "kind": "confirm"}], title="需要你确认")
        return bool(out.get("ok"))

    def log(self, msg: Any) -> None:
        self.logs.append(f"[{self._step}] {msg}")

    def fail(self, reason: str) -> None:
        raise StepFailure(reason)


# ── 序列化（与 no_agent.tools.base 的 to_jsonable / truncate 行为一致；本模块不能 import 它们）──


def jsonable(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(obj)


def clip(obj: Any, limit: int) -> Any:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return obj
    return text[:limit] + f"...[截断，共 {len(text)} 字符]"


STATE_SNAPSHOT_LIMIT = 1500


@dataclass
class Snapshot:
    """执行结束（或被终止）时交回宿主的数据，全部可 JSON 序列化"""

    result: Any = None
    has_result: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)


def snapshot(ctx: ScriptContext) -> Snapshot:
    return Snapshot(
        result=jsonable(ctx.result),
        has_result=ctx.result is not None,
        state={k: clip(jsonable(v), STATE_SNAPSHOT_LIMIT) for k, v in ctx.state.items()},
        logs=list(ctx.logs),
    )


# ── 步骤元数据（宿主以静态解析为准，不依赖脚本运行时的自我报告）──


@dataclass
class StepMeta:
    name: str
    idempotent: bool = True
    kind: str = "code"


def step_meta(source: str) -> list[StepMeta]:
    """静态解析 @step 的名字与选项；选项不是字面量时按最保守处理（视为有不可重复的副作用）"""
    out: list[StepMeta] = []
    for node in ast.parse(source, filename=SCRIPT_FILENAME).body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decos = [d for d in node.decorator_list if _is_step_decorator(d)]
        if not decos:
            continue
        meta = StepMeta(name=node.name)
        d = decos[0]
        if isinstance(d, ast.Call):
            for kw in d.keywords:
                value = kw.value.value if isinstance(kw.value, ast.Constant) else None
                if kw.arg == "idempotent":
                    meta.idempotent = value if isinstance(value, bool) else False
                elif kw.arg == "kind" and isinstance(value, str):
                    meta.kind = value
        out.append(meta)
    return out


# ── 执行单个阶段 ──

Phase = Literal["guard", "step", "verify"]
# ok：完成 | rejected：guard / verify 判定不通过（message 为原因）
# fail：ctx.fail | tool_error：工具调用失败 | abort：用户取消 | timeout：脚本自己抛出的超时 | error：其他异常
# no_result：没有 verify，且脚本没有产生结果
Status = Literal["ok", "rejected", "fail", "tool_error", "abort", "timeout", "error", "no_result"]


@dataclass
class PhaseOutcome:
    status: Status
    message: str = ""


def format_exc(e: BaseException) -> str:
    """异常信息 + 脚本内的出错行号（帮助 LLM 精确定位）"""
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == SCRIPT_FILENAME]
    where = f"（脚本第 {frames[-1].lineno} 行）" if frames else ""
    return f"{type(e).__name__}: {e}{where}"


@dataclass
class ErrorKinds:
    """把异常归类所用的异常类型（宿主与 worker 各自的工具异常 / 用户取消类型不同）"""

    tool_error: tuple[type[BaseException], ...] = ()
    abort: tuple[type[BaseException], ...] = ()


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    v = fn(*args)
    return await v if inspect.isawaitable(v) else v


async def run_phase(loaded: LoadedScript, ctx: ScriptContext, phase: Phase, name: str, kinds: ErrorKinds) -> PhaseOutcome:
    """
    执行 guard / 某一步 / verify，并把结果归类。超时由调用方（宿主）负责：
    它会取消这个协程（进程内）或直接终止 worker（子进程）。
    """
    if phase == "guard":
        ctx._step = GUARD_STEP
        try:
            g = await _call(loaded.guard, ctx) if loaded.guard else True
        except asyncio.CancelledError:
            raise
        except kinds.abort as e:
            return PhaseOutcome("abort", str(e))
        except StepFailure as e:
            return PhaseOutcome("rejected", str(e))
        except kinds.tool_error as e:
            return PhaseOutcome("rejected", f"工具调用失败: {e}")
        except asyncio.TimeoutError:
            return PhaseOutcome("timeout")
        except Exception as e:  # noqa: BLE001
            return PhaseOutcome("rejected", f"前置检查出错: {format_exc(e)}")
        if g is True or g is None:
            return PhaseOutcome("ok")
        return PhaseOutcome("rejected", g if isinstance(g, str) else "返回 False")

    if phase == "verify":
        ctx._step = VERIFY_STEP
        if loaded.verify is None:
            return PhaseOutcome("ok") if ctx.result is not None else PhaseOutcome("no_result")
        try:
            v = await _call(loaded.verify, ctx, ctx.result)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return PhaseOutcome("error", format_exc(e))
        if v is False:
            return PhaseOutcome("rejected", "")
        if isinstance(v, str):
            return PhaseOutcome("rejected", v)
        return PhaseOutcome("ok")

    step = next((s for s in loaded.steps if s.name == name), None)
    if step is None:
        return PhaseOutcome("error", f"脚本中没有步骤 {name}")
    ctx._step = step.name
    try:
        ret = await step.func(ctx)
    except asyncio.CancelledError:
        raise
    except kinds.abort as e:
        return PhaseOutcome("abort", str(e))
    except StepFailure as e:
        return PhaseOutcome("fail", str(e))
    except kinds.tool_error as e:
        return PhaseOutcome("tool_error", str(e))
    except asyncio.TimeoutError:
        return PhaseOutcome("timeout")
    except Exception as e:  # noqa: BLE001
        return PhaseOutcome("error", format_exc(e))
    if ret is not None:
        ctx.result = ret
    return PhaseOutcome("ok")
