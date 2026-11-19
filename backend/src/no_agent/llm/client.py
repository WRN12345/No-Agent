"""
LLM 客户端（OpenAI 兼容协议）

- BaseLLM：统一接口，自动按 purpose 记录 token 用量
- OpenAICompatLLM：真实实现，可接 OpenAI / Claude / 任意兼容网关
- metered()：在一次运行内统计 token 消耗
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class UsageMeter:
    """一次运行内的 token 统计，按用途细分"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_purpose: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, purpose: str, reply: LLMReply) -> None:
        self.prompt_tokens += reply.prompt_tokens
        self.completion_tokens += reply.completion_tokens
        self.calls += 1
        p = self.by_purpose.setdefault(purpose or "other", {"calls": 0, "tokens": 0})
        p["calls"] += 1
        p["tokens"] += reply.prompt_tokens + reply.completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "by_purpose": self.by_purpose,
        }


_meter: contextvars.ContextVar[UsageMeter | None] = contextvars.ContextVar("llm_meter", default=None)


@contextlib.contextmanager
def metered() -> Iterator[UsageMeter]:
    """在当前上下文中统计 LLM 用量"""
    meter = UsageMeter()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


class BaseLLM:
    """LLM 统一接口"""

    model: str = ""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        purpose: str = "",
    ) -> LLMReply:
        reply = await self._complete(messages, tools=tools, purpose=purpose)
        meter = _meter.get()
        if meter is not None:
            meter.add(purpose, reply)
        return reply

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        purpose: str,
    ) -> LLMReply:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OpenAICompatLLM(BaseLLM):
    """OpenAI 兼容协议客户端"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        temperature: float | None = 0.0,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "EMPTY", timeout=timeout)

    async def _complete(self, messages, *, tools, purpose) -> LLMReply:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if tools:
            kwargs["tools"] = tools
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_loads_args(tc.function.arguments))
            for tc in (msg.tool_calls or [])
            if getattr(tc, "function", None)
        ]
        usage = resp.usage
        return LLMReply(
            content=msg.content or "",
            tool_calls=calls,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]


def _loads_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except json.JSONDecodeError:
        return {"_raw": raw}


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(text: str) -> dict[str, Any]:
    """从模型输出中解析 JSON 对象（容忍 markdown 代码块和前后废话）"""
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    val = json.loads(text[start : end + 1])
    if not isinstance(val, dict):
        raise ValueError("JSON 不是对象")
    return val


async def complete_json(
    llm: BaseLLM,
    system: str,
    user: str,
    *,
    purpose: str,
    retries: int = 1,
) -> dict[str, Any]:
    """请求 JSON 输出，解析失败时让模型自我纠正"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err: Exception | None = None
    for _ in range(retries + 1):
        reply = await llm.complete(messages, purpose=purpose)
        try:
            return parse_json(reply.content)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            messages.append({"role": "assistant", "content": reply.content})
            messages.append({"role": "user", "content": f"上一条回复无法解析为 JSON（{e}）。只输出一个 JSON 对象。"})
    raise ValueError(f"LLM 未返回合法 JSON: {last_err}")


@dataclass
class LLMs:
    """按用途分层的模型集合"""

    fast: BaseLLM
    strong: BaseLLM
    embedder: BaseLLM | None = None


def build_llms(cfg) -> LLMs:
    """根据 LLMConfig 构建分层模型"""
    common = {"timeout": cfg.timeout, "temperature": cfg.temperature}
    fast = OpenAICompatLLM(cfg.fast_model, cfg.base_url, cfg.api_key, **common)
    strong = OpenAICompatLLM(cfg.strong_model, cfg.strong_base_url or cfg.base_url, cfg.strong_api_key or cfg.api_key, **common)
    embedder = OpenAICompatLLM(cfg.embed_model, cfg.base_url, cfg.api_key, **common) if cfg.embed_model else None
    return LLMs(fast=fast, strong=strong, embedder=embedder)
