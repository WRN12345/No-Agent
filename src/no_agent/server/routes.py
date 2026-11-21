"""API 端点：create_router(agent) 可挂载到任意 FastAPI 应用"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from no_agent.agent import Agent
from no_agent.server.schemas import (
    FeedbackRequest,
    RunDetail,
    RunRequest,
    ReviewDecision,
    RunResponse,
    SkillRunRequest,
    SkillSummary,
    VersionRequest,
)
from no_agent.skills.models import Skill


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_auth(api_token: str | None, *, local_only: bool = False) -> Callable[[Request], None]:
    """
    鉴权依赖：
      api_token 非空 → 要求 Authorization: Bearer <api_token>
      否则 local_only=True → 只接受本机（回环地址）请求
    """

    def check(request: Request) -> None:
        if api_token:
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), api_token.encode()):
                raise HTTPException(401, "需要有效的 API token（Authorization: Bearer <token>）", headers={"WWW-Authenticate": "Bearer"})
        elif local_only:
            host = request.client.host if request.client else ""
            if not _is_loopback(host):
                raise HTTPException(403, "未设置 NO_AGENT_API_TOKEN 时只接受本机请求；对外提供服务请设置 token")

    return check


def create_router(agent: Agent | Callable[[], Agent], *, api_token: str | None = None) -> APIRouter:
    """
    api_token：给定时，所有接口都要求 Authorization: Bearer <api_token>。
    不给时不做鉴权——挂到已有应用上时，由宿主应用负责（例如 include_router(..., dependencies=[...])）
    """
    get_agent: Callable[[], Agent] = agent if callable(agent) and not isinstance(agent, Agent) else (lambda: agent)  # type: ignore[assignment,return-value]
    router = APIRouter(dependencies=[Depends(make_auth(api_token))] if api_token else [])

    def _summary(a: Agent, s: Skill) -> SkillSummary:
        st = s.stats
        return SkillSummary(
            id=s.id,
            name=s.name,
            description=s.description,
            status=s.status,
            kind=s.kind,
            role=s.role,
            trust=getattr(a.store, "trust", {}).get(s.id),
            layer=a.store.layer(s.id),
            active_version=s.active_version,
            shadow_version=s.shadow_version,
            shadow_passes=s.shadow.passes if s.shadow else 0,
            shadow_failures=s.shadow.failures if s.shadow else 0,
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
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
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
        """反馈运行结果（可选接入），见 Agent.feedback"""
        try:
            return a.feedback(req.run_id, req.score, req.comment)
        except KeyError as e:
            raise HTTPException(404, "运行记录不存在") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    # ── 技能 ──

    @router.get("/skills", response_model=list[SkillSummary], tags=["skills"])
    async def list_skills(include_retired: bool = True, a: Agent = Depends(get_agent)) -> list[SkillSummary]:
        return [_summary(a, s) for s in a.store.list(include_retired=include_retired)]

    @router.get("/skills/{skill_id}", tags=["skills"])
    async def get_skill(skill_id: str, version: int | None = None, a: Agent = Depends(get_agent)) -> dict[str, Any]:
        s = _skill(a, skill_id)
        data = s.model_dump(mode="json", exclude={"tests"})
        data["layer"] = a.store.layer(skill_id)
        data["kind"] = s.kind
        data["tests"] = [{"id": t.id, "task": t.task, "params": t.params} for t in s.tests]
        if s.kind == "flow" or version:
            try:
                data["script"] = a.store.script(skill_id, version)
            except KeyError as e:
                raise HTTPException(404, str(e)) from e
        return data

    @router.post("/skills/{skill_id}/run", response_model=RunResponse, tags=["skills"])
    async def run_skill(skill_id: str, req: SkillRunRequest, a: Agent = Depends(get_agent)) -> RunResponse:
        """显式调用技能：跳过匹配，参数按 params_schema 校验"""
        _skill(a, skill_id)
        try:
            run = await a.skill(skill_id).run(req.params, task=req.task)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return RunResponse.of(run)

    @router.post("/skills/{skill_id}/activate", tags=["skills"])
    async def activate_version(skill_id: str, req: VersionRequest, a: Agent = Depends(get_agent)) -> SkillSummary:
        """回滚 / 切换到指定版本（同时重新启用）"""
        _skill(a, skill_id)
        try:
            return _summary(a, a.store.set_active(skill_id, req.version))
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @router.post("/skills/{skill_id}/promote", tags=["skills"])
    async def promote_candidate(skill_id: str, a: Agent = Depends(get_agent)) -> SkillSummary:
        """手动让影子运行中的候选版本立即转正"""
        _skill(a, skill_id)
        try:
            return _summary(a, a.store.promote(skill_id))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/skills/{skill_id}/reject", tags=["skills"])
    async def reject_candidate(skill_id: str, a: Agent = Depends(get_agent)) -> SkillSummary:
        """手动淘汰影子运行中的候选版本"""
        _skill(a, skill_id)
        try:
            return _summary(a, a.store.reject_shadow(skill_id, "手动淘汰"))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @router.get("/reviews", tags=["reviews"])
    async def list_reviews(a: Agent = Depends(get_agent)) -> list[dict[str, Any]]:
        """等待审查的版本（扩大了能力，没有自动上线）"""
        return a.reviews()

    @router.post("/skills/{skill_id}/versions/{version}/approve", tags=["reviews"])
    async def approve_version(skill_id: str, version: int, req: ReviewDecision, a: Agent = Depends(get_agent)) -> SkillSummary:
        _skill(a, skill_id)
        try:
            return _summary(a, a.approve(skill_id, version, req.comment))
        except (ValueError, KeyError) as e:
            raise HTTPException(409, str(e)) from e

    @router.post("/skills/{skill_id}/versions/{version}/reject", tags=["reviews"])
    async def reject_version(skill_id: str, version: int, req: ReviewDecision, a: Agent = Depends(get_agent)) -> SkillSummary:
        _skill(a, skill_id)
        try:
            return _summary(a, a.reject(skill_id, version, req.comment or "审查未通过"))
        except (ValueError, KeyError) as e:
            raise HTTPException(409, str(e)) from e

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
