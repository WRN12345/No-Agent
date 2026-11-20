"""
技能执行器：逐步执行脚本，失败时精确报告"哪一步、什么错、当时的状态"，供 AI 接管和修补使用
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from no_agent.llm import BaseLLM, parse_json
from no_agent.skills.script import SCRIPT_FILENAME, ScriptError, load_script
from no_agent.tools.base import Toolbox, ToolError, UserAbort, to_jsonable, truncate


GUARD_STEP = "<guard>"


class StepFailure(Exception):
    """脚本主动判定失败（ctx.fail）"""


class StepContext:
    """注入给每个步骤的 ctx"""

    def __init__(self, *, task: str, params: dict[str, Any], toolbox: Toolbox, llm: BaseLLM, skills: Any = None) -> None:
        self.task = task
        self.params = params
        self.state: dict[str, Any] = {}
        self.result: Any = None
        self.logs: list[str] = []
        self._toolbox = toolbox
        self._llm = llm
        self._skills = skills  # SkillCaller；None 表示当前环境不支持调用子技能
        self._skill_allowlist: set[str] = set()
        self._step: str | None = None

    async def tool(self, name: str, **args: Any) -> Any:
        if self._step == GUARD_STEP:
            t = self._toolbox.registry.get(name)
            if t is not None and (t.has_side_effect(args) or t.needs_confirm(args) or t.interactive):
                raise StepFailure(f"guard 中只能调用只读工具，不能调用 {name}")
        return await self._toolbox.call(name, args, step=self._step)

    async def llm(self, instruction: str, input: Any = None, *, json: bool = False) -> Any:  # noqa: A002
        content = instruction
        if input is not None:
            content += "\n\n输入:\n" + (input if isinstance(input, str) else _dumps(input))
        if json:
            content += "\n\n只输出一个 JSON 对象。"
        reply = await self._llm.complete([{"role": "user", "content": content}], purpose="script_llm")
        return parse_json(reply.content) if json else reply.content.strip()

    async def skill(self, skill_id: str, **params: Any) -> Any:
        """调用另一个流程型技能（在它自己的权限下运行），返回它的结果；失败时本步骤失败"""
        from no_agent.skills.compose import SkillCallError

        if self._step == GUARD_STEP:
            raise StepFailure("guard 中不能调用子技能")
        if self._skills is None:
            raise StepFailure("当前环境不支持调用子技能")
        if self._toolbox.tools_restricted and skill_id not in self._skill_allowlist:
            raise StepFailure(f"技能未声明调用子技能 {skill_id}")
        try:
            return await self._skills.call(skill_id, params, toolbox=self._toolbox, task=self.task)
        except SkillCallError as e:
            raise StepFailure(str(e)) from e

    async def ask(self, questions: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
        """一次性向用户提出多个问题，返回 {key: 回答}"""
        return await self.tool("ask_user", questions=questions, title=title)

    async def confirm(self, message: str) -> bool:
        """向用户确认一件事，返回是否同意"""
        out = await self.ask([{"key": "ok", "label": message, "kind": "confirm"}], title="需要你确认")
        return bool(out.get("ok"))

    def log(self, msg: Any) -> None:
        self.logs.append(f"[{self._step}] {msg}")

    def fail(self, reason: str) -> None:
        raise StepFailure(reason)


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


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _format_exc(e: BaseException) -> str:
    """异常信息 + 脚本内的出错行号（帮助 LLM 精确定位）"""
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == SCRIPT_FILENAME]
    where = f"（脚本第 {frames[-1].lineno} 行）" if frames else ""
    return f"{type(e).__name__}: {e}{where}"


async def _as_coro(fn: Any, ctx: Any) -> Any:
    v = fn(ctx)
    return await v if inspect.isawaitable(v) else v


async def _run_step(coro: Any, toolbox: Toolbox, timeout: float) -> Any:
    """执行一个步骤；超时只计算"自己在忙"的时间，等待用户输入的时间不算"""
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
) -> ExecutionResult:
    """
    skills：SkillCaller，提供 ctx.skill()；skill_allowlist：本技能声明可调用的子技能
    （仅在 toolbox 有工具限制时生效）
    """
    try:
        loaded = load_script(source)
    except ScriptError as e:
        return ExecutionResult(success=False, failed_step="<load>", error=str(e))

    ctx = StepContext(task=task, params=params, toolbox=toolbox, llm=llm, skills=skills)
    ctx._skill_allowlist = set(skill_allowlist or [])
    res = ExecutionResult(success=False, all_steps=[s.name for s in loaded.steps])

    def finish(ok: bool, step: str | None = None, error: str | None = None) -> ExecutionResult:
        res.success, res.failed_step, res.error = ok, step, error
        res.result = to_jsonable(ctx.result)
        res.state = {k: truncate(to_jsonable(v), 1500) for k, v in ctx.state.items()}
        res.logs = ctx.logs
        return res

    if loaded.guard is not None:
        ctx._step = GUARD_STEP
        try:
            g = await _run_step(_as_coro(loaded.guard, ctx), toolbox, step_timeout)
        except UserAbort as e:
            res.aborted = True
            return finish(False, GUARD_STEP, f"用户取消: {e}")
        except asyncio.TimeoutError:
            g = f"前置检查超时（{step_timeout}s）"
        except StepFailure as e:
            g = str(e)
        except ToolError as e:
            g = f"工具调用失败: {e}"
        except Exception as e:  # noqa: BLE001
            g = f"前置检查出错: {_format_exc(e)}"
        if g is not True and g is not None:
            res.guard_rejected = True
            return finish(False, GUARD_STEP, f"前置检查未通过: {g if isinstance(g, str) else '返回 False'}")

    for s in loaded.steps:
        ctx._step = s.name
        t0 = time.perf_counter()
        error: str | None = None
        try:
            ret = await _run_step(s.func(ctx), toolbox, step_timeout)
            if ret is not None:
                ctx.result = ret
        except UserAbort as e:
            res.steps.append(StepRecord(name=s.name, ok=False, ms=int((time.perf_counter() - t0) * 1000), idempotent=s.idempotent, error=str(e)))
            res.aborted = True
            return finish(False, s.name, f"用户取消: {e}")
        except StepFailure as e:
            error = f"主动失败: {e}"
        except ToolError as e:
            error = f"工具调用失败: {e}"
        except asyncio.TimeoutError:
            error = f"步骤超时（{step_timeout}s）"
        except Exception as e:  # noqa: BLE001
            error = _format_exc(e)
        ms = int((time.perf_counter() - t0) * 1000)
        res.steps.append(StepRecord(name=s.name, ok=error is None, ms=ms, idempotent=s.idempotent, error=error))
        if error:
            return finish(False, s.name, error)

    ctx._step = "verify"
    if loaded.verify is not None:
        try:
            v = loaded.verify(ctx, ctx.result)
            if inspect.isawaitable(v):
                v = await v
        except Exception as e:  # noqa: BLE001
            return finish(False, "verify", f"verify 出错: {_format_exc(e)}")
        if v is False:
            return finish(False, "verify", "verify 未通过")
        if isinstance(v, str):
            return finish(False, "verify", f"verify 未通过: {v}")
    elif ctx.result is None:
        return finish(False, "verify", "脚本没有产生结果")

    return finish(True)
