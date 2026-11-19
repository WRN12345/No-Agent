"""脚本格式、校验、执行、修补的单元测试"""

import pytest

from no_agent.llm import BaseLLM
from no_agent.skills.runtime import execute_script
from no_agent.skills.script import ScriptError, function_source, replace_functions, validate_script
from no_agent.tools.base import ReplayToolbox, Toolbox, ToolRegistry, tool

SCRIPT = '''
import re

@step
async def load(ctx):
    ctx.state["n"] = ctx.params["n"]


@step(idempotent=False)
async def double(ctx):
    if ctx.state["n"] < 0:
        ctx.fail("n 不能为负")
    return {"value": ctx.state["n"] * 2}


def verify(ctx, result):
    return result["value"] >= 0 or "结果为负"
'''


class NoLLM(BaseLLM):
    async def _complete(self, messages, *, tools, purpose):
        raise AssertionError("不应调用 LLM")


@pytest.mark.parametrize(
    "src,msg",
    [
        ("import os\n@step\nasync def a(ctx):\n    pass\n", "不允许 import os"),
        ("@step\nasync def a(ctx):\n    open('x')\n", "不允许使用 open"),
        ("@step\nasync def a(ctx):\n    return ctx.__class__\n", "私有属性"),
        ("@step\nasync def a(ctx):\n    return ctx._toolbox\n", "私有属性"),
        ("print('top level')\n@step\nasync def a(ctx):\n    pass\n", "模块顶层"),
        ("def helper():\n    pass\n", "没有任何 @step"),
        ("@step\ndef a(ctx):\n    pass\n", "必须是 async def"),
    ],
)
def test_validate_rejects(src, msg):
    with pytest.raises(ScriptError, match=msg):
        validate_script(src)


def test_validate_ok():
    assert validate_script(SCRIPT) == ["load", "double"]


async def test_execute_success():
    res = await execute_script(SCRIPT, task="t", params={"n": 3}, toolbox=Toolbox(ToolRegistry()), llm=NoLLM())
    assert res.success and res.result == {"value": 6}
    assert [s.name for s in res.steps] == ["load", "double"]
    assert res.steps[1].idempotent is False


async def test_execute_reports_failed_step():
    res = await execute_script(SCRIPT, task="t", params={"n": -1}, toolbox=Toolbox(ToolRegistry()), llm=NoLLM())
    assert not res.success
    assert res.failed_step == "double"
    assert "n 不能为负" in res.error
    assert res.state == {"n": -1}


async def test_execute_exception_has_line_number():
    res = await execute_script(SCRIPT, task="t", params={}, toolbox=Toolbox(ToolRegistry()), llm=NoLLM())
    assert res.failed_step == "load"
    assert "KeyError" in res.error and "脚本第 6 行" in res.error


def test_replace_functions_only_touches_target():
    new = replace_functions(SCRIPT, {"double": "@step\nasync def double(ctx):\n    return {'value': 0}"})
    assert "return {'value': 0}" in new
    assert function_source(new, "load") == function_source(SCRIPT, "load")
    assert validate_script(new) == ["load", "double"]


def test_replace_functions_inserts_new_step_before_verify():
    new = replace_functions(SCRIPT, {"extra": "@step\nasync def extra(ctx):\n    pass"})
    assert validate_script(new) == ["load", "double", "extra"]
    assert new.index("async def extra") < new.index("def verify")


async def test_replay_toolbox_blocks_side_effects_and_replays_fixtures():
    hits = []

    @tool("read", parameters={"type": "object", "properties": {"k": {"type": "string"}}})
    def read(k):
        hits.append(("read", k))
        return f"live-{k}"

    @tool("write", side_effect=True)
    def write():
        hits.append(("write",))

    box = ReplayToolbox(ToolRegistry([read, write]), [{"tool": "read", "args": {"k": "a"}, "ok": True, "output": "recorded"}])
    assert await box.call("read", {"k": "a"}) == "recorded"
    assert await box.call("read", {"k": "b"}) == "live-b"
    assert (await box.call("write", {}))["dry_run"] is True
    assert hits == [("read", "b")]
