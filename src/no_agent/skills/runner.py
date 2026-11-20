"""
脚本执行器（Runner）：脚本在哪里执行

宿主（execute_script）负责编排：阶段顺序、计时与超时、步骤记录、失败归因。
Runner 只负责"在某处加载脚本、按指令执行单个阶段、交回快照"：

    InProcessRunner   在宿主进程内执行（当前实现）
    ProcessRunner     在一次性子进程中执行（计划中，见 docs/design/script-isolation.html）

脚本对外部世界的一切请求都经由 CtxBridge，它始终运行在宿主：
权限检查、确认、录制、LLM 调用、子技能调用都不会进入脚本所在的进程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from no_agent.llm import BaseLLM, parse_json
from no_agent.skills.sandbox import (
    GUARD_STEP,
    ErrorKinds,
    LoadedScript,
    Phase,
    PhaseOutcome,
    ScriptContext,
    ScriptError,
    Snapshot,
    StepFailure,
    StepMeta,
    load_script,
    run_phase,
    snapshot,
    step_meta,
)
from no_agent.tools.base import Toolbox, ToolError, UserAbort

if TYPE_CHECKING:
    from no_agent.config import AgentConfig
    from no_agent.skills.compose import SkillCaller


@dataclass
class Budget:
    """
    一次执行（含它调用的所有子技能）的调用次数上限，防止脚本刷爆 LLM 费用或无限调用工具。
    None 表示不限制。子技能共用调用方的同一个 Budget。
    """

    max_tool_calls: int | None = None
    max_llm_calls: int | None = None
    tool_calls: int = 0
    llm_calls: int = 0

    @classmethod
    def from_config(cls, cfg: AgentConfig) -> Budget:
        return cls(max_tool_calls=cfg.script_max_tool_calls, max_llm_calls=cfg.script_max_llm_calls)

    def charge_tool(self) -> None:
        self.tool_calls += 1
        if self.max_tool_calls is not None and self.tool_calls > self.max_tool_calls:
            raise StepFailure(f"超出单次执行的工具调用上限（{self.max_tool_calls} 次）")

    def charge_llm(self) -> None:
        self.llm_calls += 1
        if self.max_llm_calls is not None and self.llm_calls > self.max_llm_calls:
            raise StepFailure(f"超出单次执行的 ctx.llm 调用上限（{self.max_llm_calls} 次）")


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


class CtxBridge:
    """
    ctx.tool / ctx.llm / ctx.skill 的宿主侧实现。
    当前阶段（step）由宿主编排器设置，而不是由脚本上报——guard 的只读限制据此执行。
    """

    def __init__(
        self,
        *,
        toolbox: Toolbox,
        llm: BaseLLM,
        task: str,
        skills: SkillCaller | None = None,
        skill_allowlist: list[str] | None = None,
        runner: ScriptRunner | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.toolbox = toolbox
        self.runner = runner  # 子技能沿用同一个 runner
        self.budget = budget or Budget()  # 子技能共用同一个预算
        self.llm_client = llm
        self.task = task
        self.skills = skills  # None 表示当前环境不支持调用子技能
        self.skill_allowlist = set(skill_allowlist or [])
        self.step: str | None = None

    async def tool(self, name: str, args: dict[str, Any]) -> Any:
        if self.step == GUARD_STEP:
            t = self.toolbox.registry.get(name)
            if t is None or t.has_side_effect(args) or t.needs_confirm(args) or t.interactive:
                raise StepFailure(f"guard 中只能调用只读工具，不能调用 {name}")
        self.budget.charge_tool()
        return await self.toolbox.call(name, args, step=self.step)

    async def llm(self, instruction: str, input: Any, json: bool) -> Any:  # noqa: A002
        content = instruction
        if input is not None:
            content += "\n\n输入:\n" + (input if isinstance(input, str) else _dumps(input))
        if json:
            content += "\n\n只输出一个 JSON 对象。"
        self.budget.charge_llm()
        reply = await self.llm_client.complete([{"role": "user", "content": content}], purpose="script_llm")
        return parse_json(reply.content) if json else reply.content.strip()

    async def skill(self, skill_id: str, params: dict[str, Any]) -> Any:
        from no_agent.skills.compose import SkillCallError

        if self.step == GUARD_STEP:
            raise StepFailure("guard 中不能调用子技能")
        if self.skills is None:
            raise StepFailure("当前环境不支持调用子技能")
        if self.toolbox.tools_restricted and skill_id not in self.skill_allowlist:
            raise StepFailure(f"技能未声明调用子技能 {skill_id}")
        try:
            return await self.skills.call(
                skill_id, params, toolbox=self.toolbox, task=self.task, runner=self.runner, budget=self.budget
            )
        except SkillCallError as e:
            raise StepFailure(str(e)) from e


class ScriptSession(Protocol):
    """一次脚本执行：加载完成后，按宿主的指令逐个执行阶段"""

    steps: list[StepMeta]  # 宿主静态解析的结果

    async def run(self, phase: Phase, name: str) -> PhaseOutcome:
        """执行一个阶段。宿主超时后会取消这个协程，实现方必须保证取消即终止脚本"""
        ...

    def snapshot(self) -> Snapshot: ...
    async def close(self) -> None: ...


class ScriptRunner(Protocol):
    async def open(self, source: str, *, task: str, params: dict[str, Any], bridge: CtxBridge) -> ScriptSession:
        """加载脚本；不合法时抛 ScriptError"""
        ...


# ── 进程内执行 ──

HOST_ERROR_KINDS = ErrorKinds(tool_error=(ToolError,), abort=(UserAbort,))


class InProcessSession:
    def __init__(self, loaded: LoadedScript, ctx: ScriptContext, steps: list[StepMeta]) -> None:
        self._loaded = loaded
        self._ctx = ctx
        self.steps = steps

    async def run(self, phase: Phase, name: str) -> PhaseOutcome:
        return await run_phase(self._loaded, self._ctx, phase, name, HOST_ERROR_KINDS)

    def snapshot(self) -> Snapshot:
        return snapshot(self._ctx)

    async def close(self) -> None:
        return None


class InProcessRunner:
    """在宿主进程内执行脚本。沙箱只是 Python 层的白名单，不是安全边界"""

    async def open(self, source: str, *, task: str, params: dict[str, Any], bridge: CtxBridge) -> InProcessSession:
        loaded = load_script(source)
        steps = step_meta(source)
        if [s.name for s in steps] != [s.name for s in loaded.steps]:
            raise ScriptError("步骤列表与静态解析不一致（步骤必须是顶层的 @step async def）")
        return InProcessSession(loaded, ScriptContext(task=task, params=params, bridge=bridge), steps)


IN_PROCESS = InProcessRunner()


def runner_for(cfg: AgentConfig) -> ScriptRunner:
    """按配置选择脚本执行位置"""
    if cfg.script_isolation == "inprocess":
        return IN_PROCESS
    from no_agent.skills.process import ProcessRunner, get_pool

    return ProcessRunner(get_pool(cfg.worker_pool_size, cfg.worker_memory_mb, cfg.worker_cpu_seconds))
