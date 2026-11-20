"""
技能匹配：本地召回 → 小模型判定 + 参数抽取（一次调用完成）

- 召回：配置了 embedding 模型用向量，否则用词法相似度（中文按二元组切分），零 token
- 没有召回候选时直接返回 None，不调用 LLM
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from no_agent.llm import BaseLLM, complete_json
from no_agent.skills.models import Skill

if TYPE_CHECKING:
    from no_agent.storage.base import SkillStore

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[一-鿿぀-ヿ가-힯]+")


def tokenize(text: str) -> set[str]:
    text = text.lower()
    tokens = {w for w in _WORD.findall(text) if len(w) > 1}
    for seg in _CJK.findall(text):
        if len(seg) == 1:
            tokens.add(seg)
        tokens.update(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def lexical_similarity(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / math.sqrt(len(ta) * len(tb))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class MatchResult:
    skill: Skill
    params: dict[str, Any]
    confidence: float
    reason: str = ""


MATCH_SYSTEM = """你是技能路由器。判断用户任务能否由下列某个"技能"（可复用的自动化脚本）完整完成，并从任务中抽取该技能所需的参数。

规则：
- 只有当技能确实覆盖整个任务、且所有必填参数都能从任务中确定时才选择它
- 任务只是部分相关、或需要额外步骤时，选 null
- 参数必须符合技能的 params_schema，值从任务原文中抽取，不要编造

只输出 JSON：{"skill_id": "技能id或null", "confidence": 0到1之间的数, "params": {}, "reason": "一句话理由"}"""


class SkillMatcher:
    def __init__(
        self,
        store: SkillStore,
        llm: BaseLLM,
        *,
        embedder: BaseLLM | None = None,
        top_k: int = 5,
        min_score: float = 0.05,
        min_confidence: float = 0.7,
    ) -> None:
        self.store = store
        self.llm = llm
        self.embedder = embedder
        self.top_k = top_k
        self.min_score = min_score
        self.min_confidence = min_confidence
        self._emb_cache: dict[str, tuple[str, list[float]]] = {}

    async def similarity(self, a: str, b: str) -> float:
        """两段文本的相似度（供提炼时聚类使用）"""
        if self.embedder:
            try:
                va, vb = await self.embedder.embed([a, b])
                return cosine(va, vb)
            except Exception:  # noqa: BLE001
                logger.warning("embedding 失败，退回词法相似度", exc_info=True)
        return lexical_similarity(a, b)

    async def candidates(self, task: str, skills: list[Skill] | None = None) -> list[tuple[Skill, float]]:
        # 默认只匹配可以完成整个任务的技能（原子技能只作为积木被调用）
        skills = skills if skills is not None else [s for s in self.store.list() if s.role == "task"]
        if not skills:
            return []
        scored: list[tuple[Skill, float]] | None = None
        if self.embedder:
            try:
                scored = await self._embed_scores(task, skills)
            except Exception:  # noqa: BLE001
                logger.warning("embedding 召回失败，退回词法匹配", exc_info=True)
        if scored is None:
            scored = [(s, lexical_similarity(task, s.search_text())) for s in skills]
        scored = [x for x in scored if x[1] >= self.min_score]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: self.top_k]

    async def _embed_scores(self, task: str, skills: list[Skill]) -> list[tuple[Skill, float]]:
        assert self.embedder is not None
        stale = []
        for s in skills:
            h = hashlib.md5(s.search_text().encode("utf-8")).hexdigest()
            cached = self._emb_cache.get(s.id)
            if not cached or cached[0] != h:
                stale.append((s, h))
        texts = [task] + [s.search_text() for s, _ in stale]
        vecs = await self.embedder.embed(texts)
        for (s, h), v in zip(stale, vecs[1:]):
            self._emb_cache[s.id] = (h, v)
        q = vecs[0]
        return [(s, cosine(q, self._emb_cache[s.id][1])) for s in skills]

    async def match(self, task: str, only: list[Skill] | None = None) -> MatchResult | None:
        cands = [s for s, _ in await self.candidates(task, only)] if only is None else only
        if not cands:
            return None

        user = f"任务：{task}\n\n候选技能：\n" + json.dumps([s.card() for s in cands], ensure_ascii=False, indent=1)
        try:
            out = await complete_json(self.llm, MATCH_SYSTEM, user, purpose="match")
        except Exception:  # noqa: BLE001
            logger.warning("技能匹配调用失败，按无匹配处理", exc_info=True)
            return None

        sid = out.get("skill_id")
        skill = next((s for s in cands if s.id == sid), None)
        if skill is None:
            return None
        try:
            confidence = float(out.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        params = out.get("params") or {}
        if not isinstance(params, dict):
            return None
        if confidence < self.min_confidence:
            logger.info("技能 %s 置信度 %.2f 过低，不使用", sid, confidence)
            return None
        if missing := skill.missing_params(params):
            logger.info("技能 %s 缺少必填参数 %s，不使用", sid, missing)
            return None
        return MatchResult(skill=skill, params=params, confidence=confidence, reason=str(out.get("reason", "")))
