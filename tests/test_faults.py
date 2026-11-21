"""
故障注入：worker 崩溃、发送畸形帧或超大帧、不回复、在 ask_user 等待期间被杀。
宿主都要给出确定的失败结果，并且不残留进程（见 docs/design/script-isolation.html 的“验证”一节）。

大部分用例用一个假 worker 替换 _worker.py，按指定方式违反协议；ask_user 用例用真实 worker。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from no_agent.skills import process
from no_agent.skills._worker import MAX_FRAME
from no_agent.skills.process import ProcessRunner, WorkerPool, WorkerProcess, WorkerStartError
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import Tool, Toolbox, ToolRegistry
from tests.test_script import NoLLM

SRC = '@step\nasync def a(ctx):\n    return await ctx.tool("echo", text="ok")\n'

FAKE = r'''
import json, os, struct, sys, time

inp, out = sys.stdin.buffer, sys.stdout.buffer

def send(obj=None, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode()
    out.write(struct.pack(">I", len(data)) + data)
    out.flush()

def recv():
    hdr = inp.read(4)
    if len(hdr) < 4:
        sys.exit(0)
    (n,) = struct.unpack(">I", hdr)
    return json.loads(inp.read(n))

MODE = {mode!r}
if MODE == "silent_start":
    time.sleep(600)
send({{"op": "ready", "confinement": None}})
recv()
send({{"op": "loaded", "steps": ["a"]}})
recv()  # run

if MODE == "crash":
    os._exit(7)
elif MODE == "bad_json":
    send(raw=b"not json")
elif MODE == "not_object":
    send(raw=b"[1]")
elif MODE == "no_op":
    send(raw=b'{{"status": "ok"}}')
elif MODE == "oversized":
    out.write(struct.pack(">I", {max_frame} + 1))
    out.flush()
elif MODE == "truncated":
    out.write(struct.pack(">I", 100) + b'{{"op"')
    out.flush()
    os._exit(0)
elif MODE == "bad_status":
    send({{"op": "done", "status": "weird", "snapshot": {{}}}})
elif MODE == "unexpected_op":
    send({{"op": "loaded", "steps": []}})
elif MODE == "bad_call":
    send({{"op": "call", "id": 1, "kind": "tool", "name": "echo", "args": "not a dict"}})
elif MODE == "chatty_call":
    send({{"op": "call", "id": 1, "kind": "tool", "name": "slow", "args": {{}}}})
    send({{"op": "done", "status": "ok", "snapshot": {{}}}})
elif MODE == "crash_after_call":
    send({{"op": "call", "id": 1, "kind": "tool", "name": "echo", "args": {{"text": "x"}}}})
    os._exit(9)
time.sleep(600)  # 其余情况：不再回复，等宿主终止
'''


def _box(*extra: Tool) -> Toolbox:
    async def echo(text: str = ""):
        return text

    return Toolbox(ToolRegistry([Tool("echo", "e", {"type": "object"}, echo), *extra]))


class _Pool(WorkerPool):
    """记录启动过的每个 worker，用来检查有没有残留"""

    def __init__(self) -> None:
        super().__init__(0, 256, 60)
        self.spawned: list[WorkerProcess] = []

    def _spawn(self) -> WorkerProcess:
        w = super()._spawn()
        self.spawned.append(w)
        return w


def _assert_reaped(pool: _Pool) -> None:
    """所有 worker 进程都已退出，临时目录都已删除"""
    assert pool.spawned
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(w.proc.poll() is not None and not os.path.exists(w.tmp) for w in pool.spawned):
            return
        time.sleep(0.05)
    left = [(w.proc.pid, w.proc.poll(), os.path.exists(w.tmp)) for w in pool.spawned]
    pytest.fail(f"worker 有残留（pid, 退出码, 临时目录还在）: {left}")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def use(mode: str) -> _Pool:
        path = tmp_path / f"fake_{mode}.py"
        path.write_text(FAKE.format(mode=mode, max_frame=MAX_FRAME), encoding="utf-8")
        monkeypatch.setattr(process, "WORKER_PATH", Path(path))
        return _Pool()

    return use


async def _run(pool: WorkerPool, *, box: Toolbox | None = None, timeout: float = 10.0):
    runner = ProcessRunner(pool)
    return await asyncio.wait_for(
        execute_script(SRC, task="t", params={}, toolbox=box or _box(), llm=NoLLM(), step_timeout=timeout, runner=runner),
        timeout=30,  # 兜底：宿主自己卡住也要让测试失败，而不是挂起
    )


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("crash", "退出码 7"),
        ("bad_json", "不是合法的 JSON"),
        ("not_object", "消息格式错误"),
        ("no_op", "消息格式错误"),
        ("oversized", "消息过大"),
        ("truncated", "消息不完整"),
        ("bad_status", "未知的阶段状态"),
        ("unexpected_op", "意外的消息"),
        ("bad_call", "无效的请求"),
        ("crash_after_call", "worker"),
    ],
)
async def test_protocol_faults_fail_deterministically(fake, mode, expected):
    pool = fake(mode)
    res = await _run(pool)
    assert not res.success
    assert expected in (res.error or ""), res.error
    _assert_reaped(pool)


async def test_message_while_waiting_for_reply_is_a_protocol_error(fake):
    """一问一答：worker 不等回复又发来消息，宿主不能把它当成正常结果"""
    pool = fake("chatty_call")

    async def slow():
        await asyncio.sleep(5)
        return "late"

    start = time.monotonic()
    res = await _run(pool, box=_box(Tool("slow", "s", {"type": "object"}, slow)))
    assert not res.success and "意外的消息 'done'" in (res.error or ""), res.error
    assert time.monotonic() - start < 4  # 不必等慢工具返回
    _assert_reaped(pool)


async def test_silent_worker_is_killed_on_step_timeout(fake):
    pool = fake("silent")
    start = time.monotonic()
    res = await _run(pool, timeout=1.0)
    assert not res.success and "超时" in (res.error or ""), res.error
    assert time.monotonic() - start < 10
    _assert_reaped(pool)


async def test_worker_that_never_becomes_ready(fake, monkeypatch):
    monkeypatch.setattr(process, "STARTUP_TIMEOUT", 1.0)
    pool = fake("silent_start")
    with pytest.raises(WorkerStartError, match="没有就绪"):
        await _run(pool)
    _assert_reaped(pool)


async def test_worker_killed_while_waiting_for_user():
    """ask_user 等待期间 worker 被外部杀掉：用户回答后，宿主给出失败结果，而不是挂起或把回答发给已死的进程"""
    pool = _Pool()

    async def ask_user(questions=None, title=""):
        w = pool.spawned[-1]
        w.proc.kill()  # 模拟外部终止（OOM killer、管理员 kill 等），绕过宿主自己的 kill()
        await asyncio.to_thread(w.proc.wait, 10)
        return {"answer": "yes"}

    box = _box(Tool("ask_user", "a", {"type": "object"}, ask_user, interactive=True))
    src = '@step\nasync def a(ctx):\n    r = await ctx.tool("ask_user", questions=["q"])\n    return r\n'
    res = await asyncio.wait_for(
        execute_script(src, task="t", params={}, toolbox=box, llm=NoLLM(), step_timeout=10, runner=ProcessRunner(pool)),
        timeout=30,
    )
    assert not res.success
    assert "worker" in (res.error or ""), res.error
    _assert_reaped(pool)


async def test_worker_killed_while_user_never_answers():
    """ask_user 等待期间 worker 被杀、用户一直不回答：宿主不应无限期地替一个已经死掉的执行等待用户"""
    pool = _Pool()
    never = asyncio.Event()

    async def ask_user(questions=None, title=""):
        pool.spawned[-1].proc.kill()
        await never.wait()
        return {"answer": "yes"}

    box = _box(Tool("ask_user", "a", {"type": "object"}, ask_user, interactive=True))
    src = '@step\nasync def a(ctx):\n    r = await ctx.tool("ask_user", questions=["q"])\n    return r\n'
    res = await asyncio.wait_for(
        execute_script(src, task="t", params={}, toolbox=box, llm=NoLLM(), step_timeout=10, runner=ProcessRunner(pool)),
        timeout=30,
    )
    assert not res.success
    assert "worker" in (res.error or ""), res.error
    _assert_reaped(pool)
