"""
人机交互：向用户提问、请求确认

- Asker：提问方式的协议。框架只依赖它，问题显示在哪里由宿主决定
- TerminalAsker：终端交互（默认）
- ScriptedAsker：预设回答（测试、无人值守批处理）
- ask_user 工具：脚本里的 ctx.ask() 和 AI 自由执行都通过它提问
  → 问答会被录制进轨迹，提炼和测试回放时直接使用录制的回答
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from no_agent.tools.base import Tool, ToolError, UserAbort


class Question(BaseModel):
    key: str
    label: str
    kind: Literal["text", "choice", "confirm"] = "text"
    options: list[str] = []
    required: bool = True
    default: Any = None
    help: str = ""


@runtime_checkable
class Asker(Protocol):
    async def ask(self, questions: list[Question], *, title: str = "") -> dict[str, Any]:
        """一次性提出多个问题，返回 {key: 回答}。用户放弃时抛 UserAbort"""
        ...

    async def confirm(self, message: str) -> bool:
        """请求确认，返回是否同意"""
        ...


class TerminalAsker:
    """在终端里提问"""

    def __init__(self, input_fn: Callable[[str], str] = input, output: Any = None) -> None:
        self._input = input_fn
        self._out = output or sys.stdout

    def _print(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    async def _read(self, prompt: str) -> str:
        try:
            return (await asyncio.to_thread(self._input, prompt)).strip()
        except (EOFError, KeyboardInterrupt) as e:
            raise UserAbort("用户取消了输入") from e

    async def ask(self, questions: list[Question], *, title: str = "") -> dict[str, Any]:
        self._print()
        self._print(f"──── {title or '需要你提供以下信息'} ────（直接回车跳过非必填项，输入 :q 取消）")
        answers: dict[str, Any] = {}
        for q in questions:
            if q.help:
                self._print(f"  {q.help}")
            if q.kind == "choice" and q.options:
                for i, opt in enumerate(q.options, 1):
                    self._print(f"    {i}. {opt}")
            mark = " *" if q.required else ""
            default = f" [{q.default}]" if q.default not in (None, "") else ""
            suffix = " (y/n)" if q.kind == "confirm" else ""
            while True:
                raw = await self._read(f"{q.label}{mark}{suffix}{default}: ")
                if raw == ":q":
                    raise UserAbort("用户取消了输入")
                if not raw and q.default not in (None, ""):
                    raw = str(q.default)
                if not raw:
                    if q.required:
                        self._print("  （必填）")
                        continue
                    break
                value: Any = raw
                if q.kind == "confirm":
                    value = raw.lower() in {"y", "yes", "是", "确认", "1", "true"}
                elif q.kind == "choice" and q.options:
                    if raw.isdigit() and 1 <= int(raw) <= len(q.options):
                        value = q.options[int(raw) - 1]
                    elif raw not in q.options:
                        self._print("  （请输入选项编号或选项内容）")
                        continue
                answers[q.key] = value
                break
        return answers

    async def confirm(self, message: str) -> bool:
        self._print()
        self._print("──── 需要你确认 ────")
        self._print(message)
        raw = await self._read("确认执行？[y/N]: ")
        return raw.lower() in {"y", "yes", "是", "确认"}


class ScriptedAsker:
    """预设回答：answers 可以是 {key: value}，或 (questions) -> {key: value} 的函数"""

    def __init__(
        self,
        answers: dict[str, Any] | Callable[[list[Question]], dict[str, Any]] | None = None,
        *,
        confirm: bool | Callable[[str], bool] = True,
    ) -> None:
        self.answers = answers or {}
        self._confirm = confirm
        self.asked: list[list[Question]] = []
        self.confirmations: list[str] = []

    async def ask(self, questions: list[Question], *, title: str = "") -> dict[str, Any]:
        self.asked.append(questions)
        if callable(self.answers):
            return self.answers(questions)
        missing = [q.label for q in questions if q.required and q.key not in self.answers]
        if missing:
            raise UserAbort(f"没有预设回答: {', '.join(missing)}")
        return {q.key: self.answers[q.key] for q in questions if q.key in self.answers}

    async def confirm(self, message: str) -> bool:
        self.confirmations.append(message)
        return self._confirm(message) if callable(self._confirm) else self._confirm


def default_asker() -> Asker | None:
    """有交互式终端时用终端提问；否则（服务、后台任务）返回 None"""
    try:
        return TerminalAsker() if sys.stdin is not None and sys.stdin.isatty() else None
    except (AttributeError, ValueError):
        return None


ASK_TOOL_NAME = "ask_user"


def make_ask_tool(asker: Asker | None) -> Tool:
    async def ask_user(questions: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
        if asker is None:
            raise ToolError("当前环境无法询问用户（没有配置 asker）")
        qs = [Question.model_validate(q) for q in questions]
        if not qs:
            raise ToolError("questions 不能为空")
        return await asker.ask(qs, title=title)

    return Tool(
        name=ASK_TOOL_NAME,
        description=(
            "向用户提问以获取缺失的信息。务必把所有需要问的问题合并成一次调用，不要一个个问。"
            "返回 {key: 回答}。不要用它来请求提交确认（需要确认的工具会由系统自动询问）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "这组问题的说明"},
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "回答在结果中的键"},
                            "label": {"type": "string", "description": "展示给用户的问题"},
                            "kind": {"type": "string", "enum": ["text", "choice", "confirm"]},
                            "options": {"type": "array", "items": {"type": "string"}},
                            "required": {"type": "boolean"},
                            "default": {},
                        },
                        "required": ["key", "label"],
                    },
                },
            },
            "required": ["questions"],
        },
        func=ask_user,
        interactive=True,
    )
