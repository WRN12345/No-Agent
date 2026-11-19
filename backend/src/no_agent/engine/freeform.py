"""
自由执行 / 接管：LLM 通过工具调用循环完成任务

- 自由执行：没有可用技能时，AI 自己做
- 接管：技能脚本在某一步失败后，AI 从失败处继续（已知已完成的步骤和副作用，不重复执行）
两者产出同样结构的工具调用轨迹，分别用于"提炼新技能"和"修补旧技能"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from no_agent.llm import BaseLLM
from no_agent.tools.base import Toolbox, ToolError, ToolRegistry, truncate

FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "任务完成（或确定无法完成）时调用，提交最终结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "description": "是否成功完成任务"},
                "result": {"description": "最终结果：结构化数据或文本"},
                "summary": {"type": "string", "description": "一句话说明做了什么"},
            },
            "required": ["success", "result"],
        },
    },
}

FREEFORM_SYSTEM = """你是一个任务执行 Agent。通过调用工具完成用户的任务，完成后调用 finish 提交最终结果。

要求：
- 需要与外部交互（请求、读写文件、计算等）时必须调用工具，不要假装执行
- 尽量用最少的步骤完成；工具报错时分析原因并调整，不要盲目重试
- 最终结果尽量结构化（JSON 对象），使用与任务相同的语言
- 缺少必需的信息时，用 ask_user 一次性问完所有问题，不要编造，也不要一个个问
- 提交、付款、发送等不可撤销的操作由系统自动请求用户确认，你不需要再额外询问
- 操作网页时先用 browser_snapshot 获取全部字段，再用 browser_fill 一次性批量填写，不要逐个字段操作
- 无法完成时调用 finish 并设置 success=false，在 result 中说明原因"""

TAKEOVER_SYSTEM = """

## 接管说明
这个任务原本由自动化脚本「{skill_id}」(v{version}) 执行，但它失败了，现在由你接管完成。

脚本步骤：{all_steps}
已成功完成的步骤：{done_steps}
失败的步骤：{failed_step}
错误信息：{error}

失败步骤的源码：
```python
{failed_source}
```

脚本参数：{params}
失败时的状态 ctx.state：
{state}

已经发生的副作用（不要重复执行）：
{side_effects}

请从失败处继续完成任务，可以复用 state 中已有的中间结果。你的做法会被用来修复这个脚本，所以请用清晰、可重复的方式完成。"""


@dataclass
class TakeoverContext:
    skill_id: str
    version: int
    all_steps: list[str]
    done_steps: list[str]
    failed_step: str
    error: str
    failed_source: str
    params: dict[str, Any]
    state: dict[str, Any]
    side_effects: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        return TAKEOVER_SYSTEM.format(
            skill_id=self.skill_id,
            version=self.version,
            all_steps=" → ".join(self.all_steps),
            done_steps=", ".join(self.done_steps) or "无",
            failed_step=self.failed_step,
            error=self.error,
            failed_source=self.failed_source or "（无）",
            params=json.dumps(self.params, ensure_ascii=False),
            state=json.dumps(self.state, ensure_ascii=False, indent=1)[:6000],
            side_effects=json.dumps(self.side_effects, ensure_ascii=False)[:3000] if self.side_effects else "无",
        )


@dataclass
class FreeformResult:
    success: bool
    result: Any = None
    summary: str = ""
    error: str | None = None
    turns: int = 0


async def run_freeform(
    task: str,
    *,
    llm: BaseLLM,
    toolbox: Toolbox,
    registry: ToolRegistry,
    max_turns: int = 20,
    takeover: TakeoverContext | None = None,
) -> FreeformResult:
    system = FREEFORM_SYSTEM + (takeover.render() if takeover else "")
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    tools = registry.specs() + [FINISH_TOOL]
    purpose = "takeover" if takeover else "freeform"
    step_label = "takeover" if takeover else None

    for turn in range(1, max_turns + 1):
        reply = await llm.complete(messages, tools=tools, purpose=purpose)
        if not reply.tool_calls:
            # 模型直接给出文本答案（纯问答类任务）
            return FreeformResult(success=bool(reply.content.strip()), result=reply.content, turns=turn)

        messages.append(
            {
                "role": "assistant",
                "content": reply.content or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for tc in reply.tool_calls
                ],
            }
        )
        for tc in reply.tool_calls:
            if tc.name == "finish":
                a = tc.arguments
                return FreeformResult(
                    success=bool(a.get("success", True)),
                    result=a.get("result"),
                    summary=str(a.get("summary", "")),
                    error=None if a.get("success", True) else str(a.get("result")),
                    turns=turn,
                )
            try:
                out = await toolbox.call(tc.name, tc.arguments, step=step_label)
                content = json.dumps(truncate(out), ensure_ascii=False, default=str)
            except ToolError as e:
                content = f"错误: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    return FreeformResult(success=False, error=f"超过最大轮数 {max_turns}", turns=max_turns)
