"""
SKILL.md 读写（兼容 Anthropic Agent Skills 格式）

    ---
    name: fill-form            # 小写字母 / 数字 / 连字符，≤64，与目录名一致
    description: ...           # ≤1024 字符，说明做什么、何时用
    ---
    正文：给 AI（或人）看的操作指南

frontmatter 只写标准字段；本框架的扩展数据放在同目录的 no-agent.json，
所以同一个技能目录可以原样放进 Claude Code / Codex 等工具中使用。
写回时保留 frontmatter 里其他字段（license、allowed-tools、metadata 等）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from no_agent.skills.models import Skill

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024

_FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


class SkillFormatError(ValueError):
    """SKILL.md 格式不合法"""


def parse(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONT.match(text.lstrip("﻿"))
    if not m:
        raise SkillFormatError("SKILL.md 缺少 YAML frontmatter（--- ... ---）")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillFormatError(f"frontmatter 不是合法 YAML: {e}") from e
    if not isinstance(meta, dict):
        raise SkillFormatError("frontmatter 必须是键值对")
    return meta, m.group(2).strip("\n")


def render(meta: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=10_000).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def check_meta(meta: dict[str, Any]) -> list[str]:
    errors = []
    name = meta.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > NAME_MAX:
        errors.append(f"name 必须是小写字母、数字、连字符组成，且不超过 {NAME_MAX} 字符（当前: {name!r}）")
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("description 不能为空")
    elif len(desc) > DESCRIPTION_MAX:
        errors.append(f"description 不能超过 {DESCRIPTION_MAX} 字符")
    return errors


def default_body(skill: Skill, steps: list[tuple[str, str]], script_path: str | None) -> str:
    """没有人工编写的正文时，生成一份可读的指南"""
    lines = [f"# {skill.name}", "", skill.description]
    if skill.when_to_use:
        lines += ["", "## 何时使用", "", skill.when_to_use]
    props = skill.params_schema.get("properties", {})
    if props:
        required = set(skill.params_schema.get("required", []))
        lines += ["", "## 参数", ""]
        for k, v in props.items():
            mark = "（必填）" if k in required else ""
            lines.append(f"- `{k}`{mark}: {v.get('description', v.get('type', ''))}")
    if steps:
        lines += ["", "## 步骤", ""]
        lines += [f"{i}. **{name}**" + (f"：{doc}" if doc else "") for i, (name, doc) in enumerate(steps, 1)]
    if script_path:
        lines += [
            "",
            "## 自动执行",
            "",
            f"本技能带有可直接执行的流程脚本 `{script_path}`，由 No-Agent 运行（失败时自动由 AI 接管并修补）。",
            "在其他 Agent 中使用时，可以按上面的步骤手动完成。",
        ]
    return "\n".join(lines)
