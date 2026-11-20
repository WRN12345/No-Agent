"""P2：脚本在一次性子进程中执行"""

import asyncio
import time

import pytest

from no_agent.llm import BaseLLM
from no_agent.skills import process
from no_agent.skills.compose import SkillCaller
from no_agent.skills.models import Permissions, Skill
from no_agent.skills.process import ProcessRunner, WorkerPool, get_pool
from no_agent.skills.runner import Budget
from no_agent.skills.runtime import execute_script
from no_agent.storage.files import FileSkillStore
from no_agent.tools.base import Tool, Toolbox, ToolRegistry, UserAbort
from tests.test_script import NoLLM


@pytest.fixture(scope="module")
def runner():
    return ProcessRunner(get_pool(1, 256, 60))


def _box(**extra) -> Toolbox:
    async def echo(text: str = ""):
        return {"echo": text}

    async def ask_user(questions=None, title=""):
        raise UserAbort("用户关掉了窗口")

    tools = [Tool("echo", "e", {"type": "object"}, echo), Tool("ask_user", "a", {"type": "object"}, ask_user, interactive=True)]
    return Toolbox(ToolRegistry(tools), **extra)


async def run(src, runner, *, timeout=10.0, box=None, llm=None, **kw):
    return await execute_script(
        src, task="t", params={"n": 2}, toolbox=box or _box(), llm=llm or NoLLM(), step_timeout=timeout, runner=runner, **kw
    )


async def test_basic_execution_goes_through_host(runner):
    src = '''
import json

@step
async def a(ctx):
    r = await ctx.tool("echo", text="hi")
    ctx.state["r"] = r
    ctx.log("got " + json.dumps(r))
    print("写到 stderr，不会混进协议")

@step(idempotent=False)
async def b(ctx):
    return {"echo": ctx.state["r"]["echo"], "n": ctx.params["n"]}

def verify(ctx, result):
    return result["n"] == 2 or "n 不对"
'''
    box = _box()
    res = await run(src, runner, box=box)
    assert res.success and res.result == {"echo": "hi", "n": 2}
    assert res.state == {"r": {"echo": "hi"}} and res.logs == ['[a] got {"echo": "hi"}']
    assert [(s.name, s.idempotent) for s in res.steps] == [("a", True), ("b", False)]
    assert [c.tool for c in box.calls] == ["echo"] and box.calls[0].step == "a"  # 宿主录制，步骤由宿主标注


async def test_infinite_loop_is_killed_and_previous_snapshot_kept(runner):
    src = '''
@step
async def a(ctx):
    ctx.state["before"] = 1

@step
async def spin(ctx):
    ctx.state["partial"] = 1
    while True:
        pass
'''
    t0 = time.perf_counter()
    res = await run(src, runner, timeout=1.0)
    assert time.perf_counter() - t0 < 5
    assert res.failed_step == "spin" and res.error == "步骤超时（1.0s）"
    assert res.state == {"before": 1}  # 被终止的步骤里改了一半的 state 不可见


async def test_failure_kinds_match_in_process(runner):
    cases = {
        '@step\nasync def a(ctx):\n    ctx.state["x"] = 1\n    ctx.fail("数据不对")\n': ("主动失败: 数据不对", {"x": 1}),
        '@step\nasync def a(ctx):\n    await ctx.tool("missing")\n': ("工具调用失败: 未知工具: missing（可用: echo, ask_user）", {}),
        '@step\nasync def a(ctx):\n    return {}["k"]\n': ("KeyError: 'k'（脚本第 3 行）", {}),
    }
    for src, (error, state) in cases.items():
        for r in (runner, None):  # 子进程与进程内的结果完全一致
            res = await run(src, r)
            assert (res.error, res.state) == (error, state), (src, r)


async def test_user_abort_and_host_exception_types(runner):
    res = await run('@step\nasync def a(ctx):\n    await ctx.ask([{"key": "k", "label": "?"}])\n', runner)
    assert res.aborted and res.error == "用户取消: 用户关掉了窗口"

    class QuotaExceeded(Exception):
        pass

    class BrokenLLM(BaseLLM):
        async def _complete(self, messages, *, tools, purpose):
            raise QuotaExceeded("额度用完")

    res = await run('@step\nasync def a(ctx):\n    return await ctx.llm("hi")\n', runner, llm=BrokenLLM())
    assert res.error == "QuotaExceeded: 额度用完（脚本第 3 行）"  # 类型名和脚本行号与进程内一致


async def test_memory_limit(runner):
    res = await run('@step\nasync def a(ctx):\n    return len("x" * (1024 ** 3))\n', runner)
    assert not res.success and "MemoryError" in res.error


async def test_budget_is_shared_with_child_skills(tmp_path, runner):
    store = FileSkillStore(tmp_path / "skills")
    child = '@step\nasync def a(ctx):\n    await ctx.tool("echo")\n    await ctx.tool("echo")\n    return 1\n'
    store.create(Skill(id="child", name="c", description="c", role="atomic", permissions=Permissions(tools=["echo"])), child, reason="t")
    parent = '@step\nasync def a(ctx):\n    await ctx.tool("echo")\n    return await ctx.skill("child")\n'
    caller = SkillCaller(store, NoLLM())

    res = await run(parent, runner, skills=caller, budget=Budget(max_tool_calls=3))
    assert res.success and res.result == 1  # 子技能在另一个 worker 中执行

    res = await run(parent, runner, skills=caller, budget=Budget(max_tool_calls=2))
    assert not res.success and "超出单次执行的工具调用上限（2 次）" in res.error


async def test_child_permissions_enforced_on_host(tmp_path, runner):
    store = FileSkillStore(tmp_path / "skills")
    store.create(Skill(id="child", name="c", description="c", role="atomic"), '@step\nasync def a(ctx):\n    return await ctx.tool("echo")\n', reason="t")
    box = _box()
    box.restrict(["ask_user"], None)
    res = await run('@step\nasync def a(ctx):\n    return await ctx.skill("child")\n', runner, box=box,
                    skills=SkillCaller(store, NoLLM()), skill_allowlist=["child"])
    assert not res.success and "技能未声明使用工具 echo" in res.error  # 子技能未声明 → 继承调用方的限制


async def test_load_errors_do_not_start_a_worker(monkeypatch):
    spawned = []
    pool = WorkerPool(0, 256, 60)
    monkeypatch.setattr(pool, "_spawn", lambda: spawned.append(1))
    res = await run("import os\n@step\nasync def a(ctx):\n    pass\n", ProcessRunner(pool))
    assert res.failed_step == "<load>" and "不允许 import os" in res.error and spawned == []


def test_worker_environment_is_minimal(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_AGENT_LLM__API_KEY", "sk-secret")
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured.update(kw, cmd=cmd)
            raise OSError("不真的启动")

    monkeypatch.setattr(process.subprocess, "Popen", FakePopen)
    with pytest.raises(process.WorkerStartError):
        process.WorkerProcess(256, 60)
    assert "sk-secret" not in str(captured["env"]) and "NO_AGENT_LLM__API_KEY" not in captured["env"]
    assert captured["cmd"][1:4] == ["-I", "-X", "utf8"] and captured["cwd"] != str(tmp_path)


async def test_concurrent_executions(runner):
    src = '@step\nasync def a(ctx):\n    r = await ctx.tool("echo", text=str(ctx.params["n"]))\n    return r["echo"]\n'
    results = await asyncio.gather(*(execute_script(src, task="t", params={"n": i}, toolbox=_box(), llm=NoLLM(), runner=runner) for i in range(6)))
    assert [r.result for r in results] == [str(i) for i in range(6)]


async def test_agent_uses_process_runner_by_default():
    from no_agent import AgentConfig
    from no_agent.skills.runner import runner_for

    assert isinstance(runner_for(AgentConfig()), ProcessRunner)
    assert not isinstance(runner_for(AgentConfig(script_isolation="inprocess")), ProcessRunner)


def test_posix_limits_are_applied(monkeypatch):
    """Windows 上也能跑：用替身 resource 模块检查 worker 设置的限制"""
    import sys
    import types

    from no_agent.skills import _worker

    calls = {}
    fake = types.SimpleNamespace(RLIMIT_NPROC=1, RLIMIT_AS=2, RLIMIT_CPU=3, setrlimit=lambda r, v: calls.__setitem__(r, v))
    monkeypatch.setitem(sys.modules, "resource", fake)
    monkeypatch.setattr(_worker, "os", types.SimpleNamespace(name="posix"))
    _worker._apply_limits(256, 60)
    assert calls == {1: (0, 0), 2: (256 * 1024 * 1024,) * 2, 3: (60, 60)}
