"""技能数据模型"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now()


class TestCase(BaseModel):
    """回归测试用例：来自真实运行，带录制的工具调用"""

    __test__ = False  # 防止被 pytest 当作测试类收集

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    task: str
    params: dict[str, Any] = {}
    fixtures: list[dict[str, Any]] = []
    source_run_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


class VersionInfo(BaseModel):
    version: int
    created_at: datetime = Field(default_factory=_now)
    reason: str = ""
    parent: int | None = None
    test_pass_rate: float | None = None


class SkillStats(BaseModel):
    runs: int = 0
    successes: int = 0  # 脚本独立完成
    takeovers: int = 0  # 脚本失败、AI 接管
    failures: int = 0  # 接管后仍失败
    recent: list[bool] = []  # 当前版本最近的"脚本是否独立成功"窗口
    total_latency_ms: int = 0
    feedback_sum: float = 0.0
    feedback_count: int = 0
    last_used_at: datetime | None = None
    last_error: str | None = None

    def record(self, *, script_ok: bool, final_ok: bool, latency_ms: int, error: str | None, window: int) -> None:
        self.runs += 1
        if script_ok:
            self.successes += 1
        elif final_ok:
            self.takeovers += 1
        else:
            self.failures += 1
        self.recent = (self.recent + [script_ok])[-window:]
        self.total_latency_ms += latency_ms
        self.last_used_at = _now()
        if error:
            self.last_error = error

    @property
    def recent_success_rate(self) -> float | None:
        return sum(self.recent) / len(self.recent) if self.recent else None


class Skill(BaseModel):
    schema_version: int = 1
    id: str
    name: str
    description: str
    when_to_use: str = ""
    keywords: list[str] = []
    params_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    status: Literal["active", "retired"] = "active"
    active_version: int = 1
    versions: list[VersionInfo] = []
    tests: list[TestCase] = []
    stats: SkillStats = Field(default_factory=SkillStats)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def search_text(self) -> str:
        return " ".join([self.name, self.description, self.when_to_use, " ".join(self.keywords)])

    def card(self) -> dict[str, Any]:
        """给匹配器的精简描述（控制 token）"""
        return {
            "id": self.id,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "params_schema": self.params_schema,
        }

    def missing_params(self, params: dict[str, Any]) -> list[str]:
        required = self.params_schema.get("required", [])
        return [k for k in required if params.get(k) in (None, "")]
