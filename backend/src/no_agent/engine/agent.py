"""
自进化 Agent 主流程

    任务 ─► 匹配技能 ──命中──► 执行脚本 ──成功──► 完成（最快、最省）
              │                   │
              │                   └─失败─► AI 从失败步骤接管 ──► 完成 ──► [异步] 修补脚本
              │
              └─未命中──► AI 自由执行 ──► 完成 ──► [异步] 积累轨迹、提炼新技能
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from no_agent.config import AgentConfig
from no_agent.engine.evolver import Evolver, Job
from no_agent.engine.freeform import FreeformResult, TakeoverContext, run_freeform
from no_agent.llm import LLMs, metered
from no_agent.storage.base import RunStore, SkillStore
from no_agent.storage.sql import Run
from no_agent.skills.matcher import MatchResult, SkillMatcher
from no_agent.skills.runtime import ExecutionResult, execute_script
from no_agent.skills.script import function_source
from no_agent.tools.base import Toolbox, ToolRegistry

logger = logging.getLogger(__name__)

RunMode = Literal["auto", "freeform", "skill"]


class EvolvingAgent:
    def __init__(
        self,
        *,
        store: SkillStore,
        matcher: SkillMatcher,
        registry: ToolRegistry,
        llms: LLMs,
        runs: RunStore,
        evolver: Evolver,
        settings: AgentConfig,
    ) -> None:
        self.store = store
        self.matcher = matcher
        self.registry = registry
        self.llms = llms
        self.runs = runs
        self.evolver = evolver
        self.s = settings

    async def run(
        self,
        task: str,
        *,
        mode: RunMode = "auto",
        skill_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Run:
        t0 = time.perf_counter()
        toolbox = Toolbox(self.registry, timeout=self.s.tool_timeout)
        trace: dict[str, Any] = {}

        with metered() as meter:
            match = await self._select(task, mode, skill_id, params)
            exe: ExecutionResult | None = None
            ff: FreeformResult | None = None

            if match:
                trace["match"] = {
                    "skill_id": match.skill.id,
                    "version": match.skill.active_version,
                    "params": match.params,
                    "confidence": match.confidence,
                    "reason": match.reason,
                }
                source = self.store.script(match.skill.id)
                exe = await execute_script(
                    source, task=task, params=match.params, toolbox=toolbox, llm=self.llms.fast, step_timeout=self.s.step_timeout
                )
                trace["execution"] = exe.summary()
                if not exe.success:
                    logger.info("技能 %s 在 %s 失败: %s → AI 接管", match.skill.id, exe.failed_step, exe.error)
                    trace["state_at_failure"] = exe.state
                    takeover = TakeoverContext(
                        skill_id=match.skill.id,
                        version=match.skill.active_version,
                        all_steps=exe.all_steps,
                        done_steps=[s.name for s in exe.steps if s.ok],
                        failed_step=exe.failed_step or "",
                        error=exe.error or "",
                        failed_source=function_source(source, exe.failed_step or "") or "",
                        params=match.params,
                        state=exe.state,
                        side_effects=toolbox.side_effect_calls(),
                    )
                    ff = await self._freeform(task, toolbox, takeover)
            else:
                ff = await self._freeform(task, toolbox, None)

        if exe and exe.success:
            run_mode, success, result, error = "skill", True, exe.result, None
        else:
            assert ff is not None
            run_mode = "skill_takeover" if match else "freeform"
            success, result = ff.success, ff.result
            error = ff.error or (None if ff.success else "未完成")
            trace["freeform"] = {"summary": ff.summary, "turns": ff.turns}

        trace["tool_calls"] = [c.to_trace() for c in toolbox.calls]
        trace["fixtures"] = [f for c in toolbox.calls if (f := c.to_fixture())]
        trace["usage"] = meter.to_dict()
        latency = int((time.perf_counter() - t0) * 1000)

        run = self.runs.save(
            Run(
                task=task,
                mode=run_mode,
                skill_id=match.skill.id if match else None,
                skill_version=match.skill.active_version if match else None,
                success=success,
                result_json=json.dumps(result, ensure_ascii=False, default=str),
                error=error,
                trace_json=json.dumps(trace, ensure_ascii=False, default=str),
                tool_call_count=len(toolbox.calls),
                prompt_tokens=meter.prompt_tokens,
                completion_tokens=meter.completion_tokens,
                latency_ms=latency,
            )
        )

        if match:
            self._record_skill(match, exe, success, latency)
            if run_mode == "skill_takeover" and success:
                self.evolver.submit(Job("patch", run.id))
        elif success and toolbox.calls:
            # 没用工具的纯问答不值得提炼
            self.evolver.submit(Job("distill", run.id))
        return run

    async def _select(self, task: str, mode: RunMode, skill_id: str | None, params: dict | None) -> MatchResult | None:
        if mode == "freeform":
            return None
        if mode == "skill":
            skill = self.store.get(skill_id or "")
            if skill is None:
                raise KeyError(f"技能不存在: {skill_id}")
            if params is not None:
                return MatchResult(skill=skill, params=params, confidence=1.0, reason="指定技能与参数")
            # 只指定技能：让小模型抽取参数
            m = await self.matcher.match(task, only=[skill])
            return m or MatchResult(skill=skill, params={}, confidence=0.0, reason="指定技能，未能抽取参数")
        return await self.matcher.match(task)

    async def _freeform(self, task: str, toolbox: Toolbox, takeover: TakeoverContext | None) -> FreeformResult:
        return await run_freeform(
            task,
            llm=self.llms.strong,
            toolbox=toolbox,
            registry=self.registry,
            max_turns=self.s.freeform_max_turns,
            takeover=takeover,
        )

    def _record_skill(self, match: MatchResult, exe: ExecutionResult | None, success: bool, latency: int) -> None:
        skill = self.store.get(match.skill.id)
        if skill is None:
            return
        script_ok = bool(exe and exe.success)
        skill.stats.record(
            script_ok=script_ok,
            final_ok=success,
            latency_ms=latency,
            error=None if script_ok or exe is None else f"{exe.failed_step}: {exe.error}",
            window=self.s.retire_window,
        )
        rate = skill.stats.recent_success_rate
        if len(skill.stats.recent) >= self.s.retire_window and rate is not None and rate < self.s.retire_min_success_rate:
            skill.status = "retired"
            logger.warning("技能 %s 最近成功率 %.0f%%，自动下线", skill.id, rate * 100)
            self.store.save(skill)
        else:
            self.store.save_stats(skill)
