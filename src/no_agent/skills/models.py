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


class Review(BaseModel):
    """自动上线前的变更审查：新版本扩大了能力时，等人批准后才进入影子运行 / 上线"""

    kind: Literal["patch", "distill", "upgrade"] = "patch"
    reasons: list[str] = []  # 触发审查的原因（为空则不需要审查）
    base_version: int | None = None  # 修补时比较的基准版本
    added_tools: list[str] = []
    added_skills: list[str] = []
    dynamic_calls: list[str] = []
    added_imports: list[str] = []  # 仅记录
    llm_calls_before: int | None = None  # 仅记录
    llm_calls_after: int = 0
    atoms: list[str] = []  # 提炼时一起新建、随本次审查一起批准 / 拒绝的原子技能
    parent: str | None = None  # 原子技能：由父技能的审查一起决定，不能单独批准
    requested_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None
    decision: Literal["approved", "rejected"] | None = None
    comment: str = ""


class VersionInfo(BaseModel):
    version: int
    created_at: datetime = Field(default_factory=_now)
    reason: str = ""
    parent: int | None = None
    test_pass_rate: float | None = None
    # candidate：影子运行中 | active：当前版本 | superseded：曾经上线、已被替换 | rejected：影子运行或审查未通过
    # pending_review：扩大了能力，等人批准
    status: Literal["candidate", "active", "superseded", "rejected", "pending_review"] = "active"
    note: str = ""
    review: Review | None = None


class ShadowRecord(BaseModel):
    run_id: str
    agree: bool
    reason: str = ""
    at: datetime = Field(default_factory=_now)


class ShadowState(BaseModel):
    """候选版本的影子运行记录"""

    version: int
    passes: int = 0
    failures: int = 0
    log: list[ShadowRecord] = []


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


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def validate_params(schema: dict[str, Any], params: dict[str, Any]) -> list[str]:
    """轻量参数校验（必填 + 顶层类型），返回错误列表"""
    errors = [f"缺少必填参数 {k}" for k in schema.get("required", []) if params.get(k) in (None, "")]
    props = schema.get("properties", {})
    for k, v in params.items():
        t = (props.get(k) or {}).get("type")
        if v is None or t not in _JSON_TYPES:
            continue
        if isinstance(v, bool) and t in ("integer", "number"):
            errors.append(f"参数 {k} 应为 {t}")
        elif not isinstance(v, _JSON_TYPES[t]):
            errors.append(f"参数 {k} 应为 {t}，实际是 {type(v).__name__}")
    return errors


class Permissions(BaseModel):
    """技能声明自己会用到什么（共享技能的安全边界，后续阶段据此做强制检查）"""

    tools: list[str] = []  # 用到的工具；空表示未声明
    domains: list[str] = []  # 会访问的域名
    skills: list[str] = []  # 会通过 ctx.skill() 调用的其他技能（声明了 tools 时必须声明）


class Skill(BaseModel):
    """
    一个技能 = 一个 SKILL.md 目录：
      SKILL.md         标准格式（name / description + 正文指南），可直接给 Claude Code / Codex 等使用
      no-agent.json    本框架的扩展数据（参数、版本、状态、权限）
      scripts/         flow_vN.py：各版本的流程脚本
      tests/cases.json 回归用例
    没有流程脚本的技能是"指南型"（kind=guide）：命中后把正文注入 AI 的提示词，由 AI 照着做
    """

    schema_version: int = 2
    id: str  # = SKILL.md 的 name（小写字母、数字、连字符）
    name: str  # 展示名
    description: str
    when_to_use: str = ""
    keywords: list[str] = []
    params_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    permissions: Permissions = Field(default_factory=Permissions)
    # active：正常使用 | shadow：新提炼的技能，影子运行中，尚未对外生效 | retired：已下线
    # pending_review：新提炼的技能扩大了能力，等人批准（不会被匹配、调用或执行）
    status: Literal["active", "shadow", "retired", "pending_review"] = "active"
    # task：可被整任务匹配 | atomic：原子技能，不直接匹配任务，作为积木被其他脚本 ctx.skill() 调用
    role: Literal["task", "atomic"] = "task"
    active_version: int = 0  # 0 = 没有流程脚本（指南型）
    versions: list[VersionInfo] = []
    shadow: ShadowState | None = None  # 有候选版本在影子运行时非空
    guide: str = ""  # SKILL.md 正文（L3 指南）
    tests: list[TestCase] = []
    stats: SkillStats = Field(default_factory=SkillStats)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def shadow_version(self) -> int | None:
        return self.shadow.version if self.shadow else None

    @property
    def pending_review(self) -> VersionInfo | None:
        """等待审查的版本（同一时刻最多一个）"""
        return next((v for v in self.versions if v.status == "pending_review"), None)

    @property
    def kind(self) -> Literal["flow", "guide"]:
        return "flow" if self.versions and self.active_version > 0 else "guide"

    def search_text(self) -> str:
        return " ".join([self.id, self.name, self.description, self.when_to_use, " ".join(self.keywords)])

    def card(self) -> dict[str, Any]:
        """给匹配器的精简描述（控制 token）"""
        card: dict[str, Any] = {"id": self.id, "description": self.description}
        if self.when_to_use:
            card["when_to_use"] = self.when_to_use
        if self.params_schema.get("properties"):
            card["params_schema"] = self.params_schema
        return card

    def missing_params(self, params: dict[str, Any]) -> list[str]:
        required = self.params_schema.get("required", [])
        return [k for k in required if params.get(k) in (None, "")]
