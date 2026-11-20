"""
技能脚本：格式、静态校验、加载、按步骤修补

脚本是一个普通的 Python 文件，由若干 @step 异步函数（按定义顺序执行）和可选的 verify 函数组成。
步骤是"修补的最小单位"：脚本在第 k 步失败时，只需要重写第 k 步。

安全说明：这里的 AST 白名单 + 受限 builtins 能挡住常见误用（import os、文件读写、反射逃逸），
但它不是强安全边界。脚本对外部世界的一切操作都应通过 ctx.tool()，
真正需要强隔离时，应把执行器放到独立进程 / 容器中运行。
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from typing import Any, Callable

SCRIPT_FILENAME = "<skill>"

ALLOWED_IMPORTS = {
    "json", "re", "math", "datetime", "time", "random", "statistics", "collections", "itertools",
    "functools", "string", "textwrap", "html", "urllib", "base64", "hashlib", "decimal",
    "fractions", "typing", "uuid", "csv", "io", "difflib", "unicodedata", "copy", "enum",
    "dataclasses", "operator", "bisect", "heapq", "zoneinfo", "calendar",
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

SCRIPT_API_DOC = '''
## 技能脚本格式

技能脚本是一个 Python 文件：
- 由若干 `@step` 装饰的 `async def 步骤名(ctx)` 组成，按定义顺序依次执行
- 任一步骤 `return` 非 None 的值即成为（当前的）最终结果，最后一个非 None 的返回值为准
- 可选定义 `def verify(ctx, result)`：返回 True 表示结果正确；返回字符串表示失败原因。强烈建议总是写
- 可选定义 `async def guard(ctx)`：在所有步骤之前运行的前置检查，确认环境符合脚本的假设（页面结构、数据格式等）。
  返回 True 表示可以执行；返回字符串表示不适用的原因（此时一个步骤都不会执行，直接交给 AI）。
  guard 里只能调用只读工具。脚本中有不可撤销的步骤时，强烈建议写 guard
- `step` 和 `ctx` 由运行时注入，无需 import
- 只能 import：json, re, math, datetime, time, collections, itertools, html, urllib, base64, hashlib, csv, io, difflib 等标准库纯计算模块
- 禁止 open/eval/exec/getattr、禁止访问以下划线开头的属性、禁止模块顶层执行逻辑（顶层只允许 import、常量、函数定义）
- 对外部世界的一切操作都必须通过 `await ctx.tool(...)`

## ctx API
- `ctx.task: str` 原始任务文本
- `ctx.params: dict` 技能参数（符合 params_schema）
- `ctx.state: dict` 步骤间共享的状态，请把中间结果放这里
- `await ctx.tool(name, **args)` 调用工具，失败抛异常
- `await ctx.llm(instruction, input=None, json=False)` 调用小模型做"需要理解"的事（抽取、分类、总结）。json=True 时返回 dict
- `await ctx.ask(questions, title="")` 向用户提问，questions 为 [{key, label, kind: text|choice|confirm, options, required}]，一次问完所有缺失信息，返回 {key: 回答}
- `await ctx.confirm(message)` 向用户确认一件事，返回 bool
- `ctx.log(msg)` 记录日志
- `ctx.fail(reason)` 主动判定失败（例如数据不符合预期），会触发 AI 接管

## @step 选项
- `@step` 或 `@step(idempotent=False)`：idempotent=False 表示该步骤有不可重复的副作用（写入、提交、发送）
- `@step(kind="llm")`：标记该步骤主要依赖 ctx.llm；稳定后可再提炼为确定性代码

## 原则
- 机械、确定的部分写成代码；只有真正需要理解的部分才用 ctx.llm
- 在关键处检查数据（状态码、字段是否存在），不符合预期就 ctx.fail，宁可失败交给 AI，也不要静默产出错误结果
- 任务中会变化的值（地址、名称、数量、日期等）必须来自 ctx.params，不要硬编码
- 标注了【需用户确认】的工具（如提交）由框架自动请求确认，脚本里不需要也不要再自己确认一遍
- 缺少必需信息时用 ctx.ask 一次性问用户，不要编造

## 示例
```python
import re

@step
async def fetch(ctx):
    r = await ctx.tool("http_request", url=ctx.params["url"])
    if r["status"] != 200:
        ctx.fail(f"HTTP {r['status']}")
    ctx.state["html"] = r["body"]

@step
async def extract(ctx):
    m = re.search(r"<title>(.*?)</title>", ctx.state["html"], re.S | re.I)
    if not m:
        ctx.fail("页面中没有 title")
    return {"url": ctx.params["url"], "title": m.group(1).strip()}

def verify(ctx, result):
    return bool(result.get("title")) or "title 为空"
```
'''.strip()


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
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    errors.append(f"第 {node.lineno} 行: 不允许 import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                errors.append(f"第 {node.lineno} 行: 不允许 from {node.module} import")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(f"第 {node.lineno} 行: 不允许使用 {node.id}")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            errors.append(f"第 {node.lineno} 行: 不允许使用 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            errors.append(f"第 {node.lineno} 行: 不允许访问私有属性 .{node.attr}")
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


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if level or name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError(f"不允许 import {name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


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


def script_tools(source: str) -> set[str]:
    """静态找出脚本中 ctx.tool("名字", ...) 调用的工具（用于权限检查与自动生成权限声明）"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tool"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("ask", "confirm"):
            names.add("ask_user")
    return names


# ── 按函数修补 ──


def _top_level_functions(source: str) -> dict[str, tuple[int, int]]:
    """函数名 → (起始行, 结束行)，1-based，包含装饰器"""
    tree = ast.parse(source)
    spans: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            spans[node.name] = (start, node.end_lineno or node.lineno)
    return spans


def function_source(source: str, name: str) -> str | None:
    span = _top_level_functions(source).get(name)
    if not span:
        return None
    lines = source.splitlines()
    return "\n".join(lines[span[0] - 1 : span[1]])


def replace_functions(source: str, replacements: dict[str, str]) -> str:
    """
    用新源码替换同名的顶层函数（含装饰器）。
    不存在的函数名：插入到 verify 之前（没有 verify 则追加到末尾）。
    """
    spans = _top_level_functions(source)
    lines = source.splitlines()

    existing = {n: s for n, s in replacements.items() if n in spans}
    new = {n: s for n, s in replacements.items() if n not in spans}

    for name in sorted(existing, key=lambda n: spans[n][0], reverse=True):
        start, end = spans[name]
        lines[start - 1 : end] = existing[name].strip("\n").splitlines()

    result = "\n".join(lines).rstrip() + "\n"
    if new:
        block = "\n\n\n".join(s.strip("\n") for s in new.values())
        spans_after = _top_level_functions(result)
        if "verify" in spans_after:
            rl = result.splitlines()
            at = spans_after["verify"][0] - 1
            rl[at:at] = block.splitlines() + ["", ""]
            result = "\n".join(rl).rstrip() + "\n"
        else:
            result = result.rstrip() + "\n\n\n" + block + "\n"
    return result


def step_docs(source: str) -> list[tuple[str, str]]:
    """[(步骤名, docstring 第一行)]，用于生成 SKILL.md 正文"""
    out = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.AsyncFunctionDef) and any(_is_step_decorator(d) for d in node.decorator_list):
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            out.append((node.name, doc[0] if doc else ""))
    return out


def numbered(source: str) -> str:
    """带行号的源码（给 LLM 定位错误用）"""
    return "\n".join(f"{i:>3} | {line}" for i, line in enumerate(source.splitlines(), 1))
