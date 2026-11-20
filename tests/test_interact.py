"""人机交互：终端提问、等待用户不计入超时"""

import asyncio
import io

import pytest

from no_agent import Question, TerminalAsker, UserAbort
from no_agent.interact import make_ask_tool
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import Toolbox, ToolRegistry
from tests.test_script import NoLLM


def scripted_input(lines):
    it = iter(lines)
    return lambda prompt: next(it)


async def test_terminal_asker_choice_required_and_confirm():
    out = io.StringIO()
    asker = TerminalAsker(scripted_input(["", "张三", "2", "y", ""]), output=out)
    answers = await asker.ask(
        [
            Question(key="name", label="姓名"),  # 第一次回车为空 → 必填，重新问
            Question(key="dept", label="部门", kind="choice", options=["财务部", "技术部"]),
            Question(key="agree", label="同意", kind="confirm"),
            Question(key="note", label="备注", required=False, default="无"),  # 回车 → 使用默认值
        ]
    )
    assert answers == {"name": "张三", "dept": "技术部", "agree": True, "note": "无"}
    assert "（必填）" in out.getvalue() and "2. 技术部" in out.getvalue()


async def test_terminal_asker_quit_and_confirm():
    with pytest.raises(UserAbort):
        await TerminalAsker(scripted_input([":q"]), output=io.StringIO()).ask([Question(key="a", label="A")])
    assert await TerminalAsker(scripted_input(["y"]), output=io.StringIO()).confirm("提交？") is True
    assert await TerminalAsker(scripted_input([""]), output=io.StringIO()).confirm("提交？") is False


class SlowLLM(NoLLM):
    async def _complete(self, messages, *, tools, purpose):
        await asyncio.sleep(1)


class SlowAsker:
    async def ask(self, questions, *, title=""):
        await asyncio.sleep(0.6)  # 用户思考很久
        return {"x": "ok"}

    async def confirm(self, message):
        return True


async def test_waiting_for_user_does_not_count_toward_step_timeout():
    src = '@step\nasync def a(ctx):\n    r = await ctx.ask([{"key": "x", "label": "X"}])\n    return r\n'
    box = Toolbox(ToolRegistry([make_ask_tool(SlowAsker())]), asker=SlowAsker())
    res = await execute_script(src, task="t", params={}, toolbox=box, llm=NoLLM(), step_timeout=0.3)
    assert res.success and res.result == {"x": "ok"}

    # 自己忙超过超时仍然会被判定超时
    busy = "@step\nasync def a(ctx):\n    return await ctx.llm('slow')\n"
    res = await execute_script(busy, task="t", params={}, toolbox=Toolbox(ToolRegistry()), llm=SlowLLM(), step_timeout=0.3)
    assert not res.success and "超时" in res.error
