"""
自动上线前的变更审查：新版本扩大了能力 → 不自动上线，等人批准

触发审查（扩大能力）：
- 新增工具：修补时以技能声明的工具为基准；没有声明时，以当前版本脚本实际用到的工具为基准。
  新技能（提炼 / 指南升级）以轨迹里真实用过的工具为基准
- 新增子技能调用（修补时；基准同上）
- 动态调用：ctx.tool / ctx.skill 的名字不是字面量，或者把 ctx.tool / ctx.skill 赋给变量再调用——
  静态分析看不到它会调用什么，等同于"可以调用任何东西"

只记录、不触发（避免审查疲劳——几乎每次修补都会碰到，白名单模块也都是纯计算）：
- 新增 import
- ctx.llm 调用点数量的变化
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field

from no_agent.skills.models import Review, Skill
from no_agent.tools.base import ALWAYS_ALLOWED

_NAMED_CALLS = {"tool", "skill"}


@dataclass
class Capabilities:
    tools: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    llm_calls: int = 0
    dynamic: list[str] = field(default_factory=list)  # 无法静态确定目标的调用（行号 + 说明）
    # 与 dynamic 一一对应的键：调用处的表达式（不含行号），用来比较两个版本"多了哪些"动态调用
    dynamic_keys: list[str] = field(default_factory=list)


def capabilities(source: str) -> Capabilities:
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    caps = Capabilities()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            caps.imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            caps.imports.add(node.module)
        elif isinstance(node, ast.Attribute) and node.attr == "llm":
            caps.llm_calls += 1
        elif isinstance(node, ast.Attribute) and node.attr in ("ask", "confirm"):
            caps.tools.add("ask_user")
        elif isinstance(node, ast.Attribute) and node.attr in _NAMED_CALLS:
            call = parents.get(id(node))
            literal = (
                isinstance(call, ast.Call)
                and call.func is node
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            )
            if literal:
                (caps.tools if node.attr == "tool" else caps.skills).add(call.args[0].value)  # type: ignore[union-attr]
            else:
                # 调用：以"调用目标"那个参数的表达式为键（改其他参数不算新的动态调用）；
                # 非调用（把 ctx.tool 赋给变量 / 传出去）：以 ctx.tool 本身为键
                key = ast.unparse(call.args[0]) if isinstance(call, ast.Call) and call.func is node and call.args else ast.unparse(node)
                caps.dynamic.append(  # type: ignore[arg-type]
                    (node.lineno, f"第 {node.lineno} 行: ctx.{node.attr} 的目标不是字面量，无法静态确定会调用什么", key)
                )
    items = sorted(caps.dynamic)  # type: ignore[arg-type]  # ast.walk 是广度优先，按行号排序
    caps.dynamic = [msg for _, msg, _ in items]  # type: ignore[misc]
    caps.dynamic_keys = [key for _, _, key in items]  # type: ignore[misc]
    return caps


def _added_dynamic(base: Capabilities, new: Capabilities) -> list[str]:
    """新版本比基准版本多出来的动态调用（按调用表达式比较，不看行号；同一个表达式出现多次按次数算）"""
    extra = Counter(new.dynamic_keys) - Counter(base.dynamic_keys)
    out = []
    for key, msg in zip(new.dynamic_keys, new.dynamic):
        if extra[key] > 0:
            extra[key] -= 1
            out.append(msg)
    return out


def _finish(review: Review) -> Review | None:
    if review.added_tools:
        review.reasons.append(f"新增工具: {', '.join(review.added_tools)}")
    if review.added_skills:
        review.reasons.append(f"新增子技能调用: {', '.join(review.added_skills)}")
    review.reasons.extend(review.dynamic_calls)
    return review if review.reasons else None


def review_patch(skill: Skill, base_version: int, base_source: str, new_source: str) -> Review | None:
    """修补：和当前版本（或正在影子运行的候选）比较。返回 None 表示没有扩大能力，可以自动上线"""
    base, new = capabilities(base_source), capabilities(new_source)
    tool_base = set(skill.permissions.tools) or base.tools
    skill_base = set(skill.permissions.skills) or base.skills
    review = Review(
        kind="patch",
        base_version=base_version,
        added_tools=sorted(new.tools - tool_base - ALWAYS_ALLOWED),
        added_skills=sorted(new.skills - skill_base),
        dynamic_calls=_added_dynamic(base, new),
        added_imports=sorted(new.imports - base.imports),
        llm_calls_before=base.llm_calls,
        llm_calls_after=new.llm_calls,
    )
    return _finish(review)


def review_new(
    kind: str, script: str, *, trace_tools: set[str], atoms: dict[str, str] | None = None, declared_tools: list[str] | None = None
) -> Review | None:
    """
    新技能（提炼 / 指南升级为脚本）：工具必须是轨迹里真实用过的（或技能已声明的），原子技能同样检查。
    调用已有的技能不触发审查：子技能在它自己声明的权限下运行。
    """
    baseline = set(declared_tools or []) or set(trace_tools)
    sources = {"": script, **(atoms or {})}
    added: set[str] = set()
    dynamic: list[str] = []
    llm = 0
    for sid, src in sources.items():
        caps = capabilities(src)
        added |= caps.tools - baseline - ALWAYS_ALLOWED
        dynamic += [f"原子技能 {sid} {d}" if sid else d for d in caps.dynamic]
        llm += caps.llm_calls
    review = Review(
        kind=kind,  # type: ignore[arg-type]
        added_tools=sorted(added),
        dynamic_calls=dynamic,
        llm_calls_after=llm,
        atoms=sorted(a for a in sources if a),
    )
    return _finish(review)


def trace_tools(runs: list) -> set[str]:
    """一组运行记录里真实调用过的工具"""
    return {str(c.get("tool")) for r in runs for c in r.trace.get("tool_calls", []) if c.get("tool")}
