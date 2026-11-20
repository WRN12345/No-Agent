"""
运行记录（执行轨迹）—— 提炼与修补的原材料

表名都带 no_agent_ 前缀，嵌入使用者已有的数据库时不会与业务表冲突。
no_agent_meta 记录存储结构版本，结构变化时在 MIGRATIONS 中登记迁移函数。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import Column, Text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

from no_agent.storage.base import RUNS_SCHEMA_VERSION as SCHEMA_VERSION

logger = logging.getLogger(__name__)


class Run(SQLModel, table=True):
    __tablename__ = "no_agent_runs"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    task: str = Field(sa_column=Column(Text, nullable=False))
    # skill：脚本独立完成 | skill_takeover：脚本失败、AI 接管 | freeform：AI 自己完成
    mode: str = Field(index=True)
    skill_id: str | None = Field(default=None, index=True)
    skill_version: int | None = None
    success: bool = False
    result_json: str = Field(default="null", sa_column=Column(Text))
    error: str | None = Field(default=None, sa_column=Column(Text))
    trace_json: str = Field(default="{}", sa_column=Column(Text))
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    feedback_score: float | None = None
    feedback_comment: str | None = Field(default=None, sa_column=Column(Text))
    distilled_into: str | None = Field(default=None, index=True)  # 已被提炼进哪个技能
    created_at: datetime = Field(default_factory=datetime.now, index=True)

    @property
    def result(self) -> Any:
        return json.loads(self.result_json)

    @property
    def trace(self) -> dict[str, Any]:
        return json.loads(self.trace_json)


class Meta(SQLModel, table=True):
    __tablename__ = "no_agent_meta"

    key: str = Field(primary_key=True)
    value: str


# 迁移：{目标版本: fn(engine)}，从 当前版本+1 依次执行到 SCHEMA_VERSION
MIGRATIONS: dict[int, Callable[[Engine], None]] = {}


def migrate(engine: Engine) -> None:
    with Session(engine) as s:
        row = s.get(Meta, "schema_version")
        current = int(row.value) if row else SCHEMA_VERSION  # 新库直接是最新版本
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"数据库结构版本 {current} 高于当前框架支持的 {SCHEMA_VERSION}，请升级 No-Agent")
        for v in range(current + 1, SCHEMA_VERSION + 1):
            if v not in MIGRATIONS:
                raise RuntimeError(f"缺少到结构版本 {v} 的迁移")
            logger.info("迁移运行记录库到结构版本 %s", v)
            MIGRATIONS[v](engine)
        s.merge(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
        s.commit()


class SqlRunStore:
    def __init__(self, url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if not url:
                raise ValueError("需要 url 或 engine")
            kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
            engine = create_engine(url, **kwargs)
        self.engine = engine
        SQLModel.metadata.create_all(engine, tables=[Run.__table__, Meta.__table__])  # type: ignore[attr-defined]
        migrate(engine)

    def save(self, run: Run) -> Run:
        with Session(self.engine, expire_on_commit=False) as s:
            merged = s.merge(run)
            s.commit()
            return merged

    def get(self, run_id: str) -> Run | None:
        with Session(self.engine, expire_on_commit=False) as s:
            return s.get(Run, run_id)

    def list(self, limit: int = 50, skill_id: str | None = None) -> list[Run]:
        with Session(self.engine, expire_on_commit=False) as s:
            q = select(Run).order_by(Run.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
            if skill_id:
                q = q.where(Run.skill_id == skill_id)
            return list(s.exec(q))

    def distill_candidates(self, limit: int = 200) -> list[Run]:
        """尚未提炼、成功、用过工具、没有负反馈的自由执行记录"""
        with Session(self.engine, expire_on_commit=False) as s:
            q = (
                select(Run)
                .where(Run.mode == "freeform", Run.success == True, Run.distilled_into == None)  # noqa: E711,E712
                .where(Run.tool_call_count > 0)
                .order_by(Run.created_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            return [r for r in s.exec(q) if r.feedback_score is None or r.feedback_score >= 0]
