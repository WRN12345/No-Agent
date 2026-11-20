"""API 请求 / 响应模型"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from no_agent.storage.sql import Run


class RunRequest(BaseModel):
    task: str = Field(..., min_length=1, description="任务描述")
    mode: Literal["auto", "freeform", "skill"] = "auto"
    skill_id: str | None = Field(None, description="mode=skill 时指定技能")
    params: dict[str, Any] | None = Field(None, description="mode=skill 时可直接给参数，省去抽取")


class RunResponse(BaseModel):
    run_id: str
    task: str
    mode: str
    success: bool
    result: Any = None
    error: str | None = None
    skill_id: str | None = None
    skill_version: int | None = None
    tool_calls: int = 0
    tokens: int = 0
    latency_ms: int = 0
    feedback_score: float | None = None
    created_at: datetime

    @classmethod
    def of(cls, run: Run) -> "RunResponse":
        return cls(
            run_id=run.id,
            task=run.task,
            mode=run.mode,
            success=run.success,
            result=run.result,
            error=run.error,
            skill_id=run.skill_id,
            skill_version=run.skill_version,
            tool_calls=run.tool_call_count,
            tokens=run.prompt_tokens + run.completion_tokens,
            latency_ms=run.latency_ms,
            feedback_score=run.feedback_score,
            created_at=run.created_at,
        )


class RunDetail(RunResponse):
    trace: dict[str, Any] = {}


class FeedbackRequest(BaseModel):
    run_id: str
    score: float = Field(..., ge=-1, le=1, description="-1 差 ~ 1 好")
    comment: str | None = Field(None, description="哪里不对；负反馈时用于修补脚本")


class SkillSummary(BaseModel):
    id: str
    name: str
    description: str
    status: str
    kind: str  # flow：带流程脚本 | guide：纯 SKILL.md 指南
    role: str = "task"  # task：可匹配整任务 | atomic：原子技能，供其他脚本调用
    trust: str | None = None  # 共享技能的信任说明（已签名 / 未签名 / 被修改）
    layer: str  # local：本项目技能库 | shared：只读共享库
    active_version: int
    shadow_version: int | None = None  # 影子运行中的候选版本
    shadow_passes: int = 0
    shadow_failures: int = 0
    versions: int
    tests: int
    runs: int
    script_success_rate: float | None
    recent_success_rate: float | None
    takeovers: int
    updated_at: datetime


class SkillRunRequest(BaseModel):
    params: dict[str, Any] | None = Field(None, description="技能参数（按 params_schema 校验）；不给则从 task 中抽取")
    task: str | None = Field(None, description="任务描述（可选）")


class VersionRequest(BaseModel):
    version: int


class ReviewDecision(BaseModel):
    comment: str = Field("", description="批准说明 / 拒绝原因")
