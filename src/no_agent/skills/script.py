"""
技能脚本：格式说明、静态分析、按步骤修补

脚本是一个普通的 Python 文件，由若干 @step 异步函数（按定义顺序执行）和可选的 verify 函数组成。
步骤是"修补的最小单位"：脚本在第 k 步失败时，只需要重写第 k 步。

沙箱（静态校验、受限加载）在 no_agent.skills.sandbox，这里重新导出以保持原有的导入路径。
"""

from __future__ import annotations

import ast

from no_agent.skills.sandbox import (  # noqa: F401 - 重新导出
    ALLOWED_IMPORTS,
    ALLOWED_SUBMODULES,
    DENIED_MEMBERS,
    FORBIDDEN_ATTRS,
    FORBIDDEN_NAMES,
    SCRIPT_FILENAME,
    LoadedScript,
    ScriptError,
    StepDef,
    _is_step_decorator,
    load_script,
    validate_script,
)

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
- 只能 import：json, re, math, datetime, time, collections, itertools, html, urllib.parse, base64, hashlib, csv, io, difflib 等标准库纯计算模块（只能使用模块里的函数、类、常量，不能经由它们访问其他模块）
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
- `await ctx.skill(skill_id, **params)` 调用另一个已有的流程型技能，返回它的结果（它在自己的权限下运行，失败时本步骤失败）。
  已有技能能做的事不要重写，直接调用
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




def script_skills(source: str) -> set[str]:
    """静态找出脚本中 ctx.skill("id", ...) 调用的子技能"""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "skill"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def count_llm_calls(source: str) -> int:
    """脚本里 ctx.llm(...) 的调用点数量（多候选排序时越少越好：更确定、更省）"""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "llm"
    )


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
