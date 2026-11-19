"""API 端点：create_router(agent) 可挂载到任意 FastAPI 应用"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from no_agent.agent import Agent
from no_agent.engine.evolver import Job
from no_agent.server.schemas import (
    FeedbackRequest,
    RunDetail,
    RunRequest,
    RunResponse,
    SkillSummary,
    VersionRequest,
)
from no_agent.skills.models import Skill


def create_router(agent: Agent | Callable[[], Agent]) -> APIRouter:
    get_agent: Callable[[], Agent] = agent if callable(agent) and not isinstance(agent, Agent) else (lambda: agent)  # type: ignore[assignment,return-value]
    router = APIRouter()

    def _summary(a: Agent, s: Skill) -> SkillSummary:
        st = s.stats
        return SkillSummary(
            id=s.id,
            name=s.name,
            description=s.description,
            status=s.status,
            layer=a.store.layer(s.id),
            active_version=s.active_version,
            versions=len(s.versions),
            tests=len(s.tests),
            runs=st.runs,
            script_success_rate=st.successes / st.runs if st.runs else None,
            recent_success_rate=st.recent_success_rate,
            takeovers=st.takeovers,
            updated_at=s.updated_at,
        )

    def _skill(a: Agent, skill_id: str) -> Skill:
        s = a.store.get(skill_id)
        if s is None:
            raise HTTPException(404, "技能不存在")
        return s

    # ── 运行 ──

    @router.post("/run", response_model=RunResponse, tags=["run"])
    async def run_task(req: RunRequest, a: Agent = Depends(get_agent)) -> RunResponse:
        """执行任务：自动选择 技能脚本 / AI 接管 / AI 自由执行"""
        try:
            run = await a.run(req.task, mode=req.mode, skill_id=req.skill_id, params=req.params)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return RunResponse.of(run)

    @router.get("/runs", response_model=list[RunResponse], tags=["run"])
    async def list_runs(limit: int = 50, skill_id: str | None = None, a: Agent = Depends(get_agent)) -> list[RunResponse]:
        return [RunResponse.of(r) for r in a.runs.list(limit=limit, skill_id=skill_id)]

    @router.get("/runs/{run_id}", response_model=RunDetail, tags=["run"])
    async def get_run(run_id: str, a: Agent = Depends(get_agent)) -> RunDetail:
        run = a.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "运行记录不存在")
        return RunDetail(**RunResponse.of(run).model_dump(), trace=run.trace)

    @router.post("/feedback", tags=["run"])
    async def feedback(req: FeedbackRequest, a: Agent = Depends(get_agent)) -> dict[str, Any]:
        """
        异步反馈（可选接入）：
        - 记录到运行记录与技能统计
        - 负反馈的自由执行轨迹不会被提炼
        - 负反馈的脚本运行（脚本自认为成功）→ 触发修补并加强 verify
        """
        run = a.runs.get(req.run_id)
        if run is None:
            raise HTTPException(404, "运行记录不存在")
        run.feedback_score, run.feedback_comment = req.score, req.comment
        a.runs.save(run)

        queued = False
        if run.skill_id and (skill := a.store.get(run.skill_id)):
            skill.stats.feedback_sum += req.score
            skill.stats.feedback_count += 1
            a.store.save_stats(skill)
            if req.score < 0 and run.mode == "skill":
                a.evolver.submit(Job("feedback", run.id))
                queued = True
        return {"ok": True, "patch_queued": queued}

    # ── 技能 ──

    @router.get("/skills", response_model=list[SkillSummary], tags=["skills"])
    async def list_skills(include_retired: bool = True, a: Agent = Depends(get_agent)) -> list[SkillSummary]:
        return [_summary(a, s) for s in a.store.list(include_retired=include_retired)]

    @router.get("/skills/{skill_id}", tags=["skills"])
    async def get_skill(skill_id: str, version: int | None = None, a: Agent = Depends(get_agent)) -> dict[str, Any]:
        s = _skill(a, skill_id)
        data = s.model_dump(mode="json", exclude={"tests"})
        data["layer"] = a.store.layer(skill_id)
        data["tests"] = [{"id": t.id, "task": t.task, "params": t.params} for t in s.tests]
        try:
            data["script"] = a.store.script(skill_id, version)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return data

    @router.post("/skills/{skill_id}/activate", tags=["skills"])
    async def activate_version(skill_id: str, req: VersionRequest, a: Agent = Depends(get_agent)) -> SkillSummary:
        """回滚 / 切换到指定版本（同时重新启用）"""
        _skill(a, skill_id)
        try:
            return _summary(a, a.store.set_active(skill_id, req.version))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @router.post("/skills/{skill_id}/retire", tags=["skills"])
    async def retire_skill(skill_id: str, a: Agent = Depends(get_agent)) -> SkillSummary:
        _skill(a, skill_id)
        return _summary(a, a.store.set_status(skill_id, "retired"))

    @router.delete("/skills/{skill_id}", tags=["skills"])
    async def delete_skill(skill_id: str, a: Agent = Depends(get_agent)) -> dict[str, bool]:
        _skill(a, skill_id)
        try:
            a.store.delete(skill_id)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return {"ok": True}

    # ── 进化器 / 工具 ──

    @router.get("/evolver", tags=["evolver"])
    async def evolver_status(a: Agent = Depends(get_agent)) -> dict[str, Any]:
        return {"queued": a.evolver.queue.qsize(), "history": list(a.evolver.history)[::-1]}

    @router.get("/tools", tags=["tools"])
    async def list_tools(a: Agent = Depends(get_agent)) -> list[dict[str, Any]]:
        return [t["function"] for t in a.registry.specs()]

    return router
