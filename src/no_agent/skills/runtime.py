"""
技能执行编排：逐阶段执行脚本（guard → 各步骤 → verify），失败时精确报告"哪一步、什么错、当时的状态"，
供 AI 接管和修补使用。

编排（顺序、计时、超时、记录、归因）始终在宿主；脚本在哪里执行由 Runner 决定（见 skills/runner.py）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable

from no_agent.llm import BaseLLM
from no_agent.skills.runner import IN_PROCESS, Budget, CtxBridge, ScriptRunner, ScriptSession
from no_agent.skills.sandbox import GUARD_STEP, VERIFY_STEP, PhaseOutcome, ScriptError, StepFailure  # noqa: F401
from no_agent.tools.base import Toolbox


@dataclass
class StepRecord:
    name: str
    ok: bool
    ms: int
    idempotent: bool = True
    error: str | None = None


@dataclass
class ExecutionResult:
    success: bool
    result: Any = None
    failed_step: str | None = None
    error: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    all_steps: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    aborted: bool = False  # 用户取消（不是脚本的错）
    guard_rejected: bool = False  # 前置检查判定"不适用"，一个步骤都没执行

    def summary(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "failed_step": self.failed_step,
            "error": self.error,
            "steps": [s.__dict__ for s in self.steps],
            "logs": self.logs[-20:],
            "aborted": self.aborted,
            "guard_rejected": self.guard_rejected,
        }


async def _run_step(coro: Awaitable[Any], toolbox: Toolbox, timeout: float) -> Any:
    """执行一个阶段；超时只计算"自己在忙"的时间，等待用户输入的时间不算。超时后取消该阶段"""
    task = asyncio.ensure_future(coro)
    start, waited0 = time.perf_counter(), toolbox.waited_total()
    while True:
        busy = time.perf_counter() - start - (toolbox.waited_total() - waited0)
        remaining = timeout - busy
        if remaining <= 0 and toolbox.waiting_since is None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise asyncio.TimeoutError
        done, _ = await asyncio.wait({task}, timeout=max(remaining, 0.2))
        if done:
            return task.result()


async def execute_script(
    source: str,
    *,
    task: str,
    params: dict[str, Any],
    toolbox: Toolbox,
    llm: BaseLLM,
    step_timeout: float = 60.0,
    skills: Any = None,
    skill_allowlist: list[str] | None = None,
    runner: ScriptRunner | None = None,
    budget: Budget | None = None,
) -> ExecutionResult:
    """
    skills：SkillCaller，提供 ctx.skill()；skill_allowlist：本技能声明可调用的子技能
    （仅在 toolbox 有工具限制时生效）；runner：脚本在哪里执行，默认进程内；
    budget：调用次数上限（子技能共用），默认不限制
    """
    runner = runner or IN_PROCESS
    bridge = CtxBridge(
        toolbox=toolbox, llm=llm, task=task, skills=skills, skill_allowlist=skill_allowlist, runner=runner, budget=budget
    )
    try:
        session = await runner.open(source, task=task, params=params, bridge=bridge)
    except ScriptError as e:
        return ExecutionResult(success=False, failed_step="<load>", error=str(e))
    try:
        return await _orchestrate(session, bridge, toolbox, step_timeout)
    finally:
        await session.close()


async def _orchestrate(session: ScriptSession, bridge: CtxBridge, toolbox: Toolbox, timeout: float) -> ExecutionResult:
    res = ExecutionResult(success=False, all_steps=[s.name for s in session.steps])

    async def phase(kind: Any, name: str, step: str) -> PhaseOutcome:
        bridge.step = step  # 宿主设置当前阶段：guard 的只读限制、工具调用录制都以此为准
        try:
            return await _run_step(session.run(kind, name), toolbox, timeout)
        except asyncio.TimeoutError:
            return PhaseOutcome("timeout")

    def finish(ok: bool, step: str | None = None, error: str | None = None) -> ExecutionResult:
        snap = session.snapshot()
        res.success, res.failed_step, res.error = ok, step, error
        res.result, res.state, res.logs = snap.result, snap.state, snap.logs
        return res

    # ── 前置检查 ──
    out = await phase("guard", GUARD_STEP, GUARD_STEP)
    if out.status == "abort":
        res.aborted = True
        return finish(False, GUARD_STEP, f"用户取消: {out.message}")
    if out.status != "ok":
        reason = f"前置检查超时（{timeout}s）" if out.status == "timeout" else out.message
        res.guard_rejected = True
        return finish(False, GUARD_STEP, f"前置检查未通过: {reason}")

    # ── 步骤 ──
    for meta in session.steps:
        t0 = time.perf_counter()
        out = await phase("step", meta.name, meta.name)
        ms = int((time.perf_counter() - t0) * 1000)
        if out.status == "abort":
            res.steps.append(StepRecord(name=meta.name, ok=False, ms=ms, idempotent=meta.idempotent, error=out.message))
            res.aborted = True
            return finish(False, meta.name, f"用户取消: {out.message}")
        error = {
            "ok": None,
            "fail": f"主动失败: {out.message}",
            "tool_error": f"工具调用失败: {out.message}",
            "timeout": f"步骤超时（{timeout}s）",
        }.get(out.status, out.message)
        res.steps.append(StepRecord(name=meta.name, ok=error is None, ms=ms, idempotent=meta.idempotent, error=error))
        if error:
            return finish(False, meta.name, error)

    # ── 结果校验 ──
    out = await phase("verify", VERIFY_STEP, VERIFY_STEP)
    if out.status == "ok":
        return finish(True)
    if out.status == "no_result":
        return finish(False, VERIFY_STEP, "脚本没有产生结果")
    if out.status == "timeout":
        return finish(False, VERIFY_STEP, f"verify 超时（{timeout}s）")
    if out.status == "rejected":
        return finish(False, VERIFY_STEP, f"verify 未通过: {out.message}" if out.message else "verify 未通过")
    return finish(False, VERIFY_STEP, f"verify 出错: {out.message}")
