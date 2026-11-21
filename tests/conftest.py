"""pytest 配置：假 LLM + 可控工具，离线跑通完整进化闭环"""

from __future__ import annotations

import json
import os
import typing
from typing import Any, Callable

import pytest

from no_agent import Agent, AgentConfig
from no_agent.llm import BaseLLM, LLMReply, LLMs, ToolCall
from no_agent.tools.base import ToolError, tool

# 用 NO_AGENT_SCRIPT_ISOLATION 切换整套测试的默认隔离档位，例如在 Linux 上跑一遍 sandbox 档：
#   NO_AGENT_SCRIPT_ISOLATION=sandbox python -m pytest
# AgentConfig() 本身不读环境变量（只有 from_env / CLI / HTTP 服务读），所以这里直接改字段默认值；
# 测试里显式指定的档位不受影响。
_ISOLATION = os.environ.get("NO_AGENT_SCRIPT_ISOLATION", "").strip()
if _ISOLATION:
    _allowed = typing.get_args(AgentConfig.model_fields["script_isolation"].annotation)
    if _ISOLATION not in _allowed:
        raise pytest.UsageError(f"NO_AGENT_SCRIPT_ISOLATION={_ISOLATION!r} 无效，可选值：{', '.join(_allowed)}")
    AgentConfig.model_fields["script_isolation"].default = _ISOLATION
    AgentConfig.model_rebuild(force=True)


def pytest_report_header(config):
    return f"script_isolation 默认档位: {AgentConfig().script_isolation}"

Handler = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], LLMReply]


class FakeLLM(BaseLLM):
    """按 purpose 分派到脚本化的处理函数，并记录调用"""

    def __init__(self, name: str) -> None:
        self.model = name
        self.handlers: dict[str, Handler] = {}
        self.calls: list[str] = []

    def on(self, purpose: str, handler: Handler) -> None:
        self.handlers[purpose] = handler

    async def _complete(self, messages, *, tools, purpose) -> LLMReply:
        self.calls.append(purpose)
        if purpose not in self.handlers:
            raise AssertionError(f"{self.model} 未预期的调用: {purpose}")
        reply = self.handlers[purpose](messages, tools)
        reply.prompt_tokens = reply.prompt_tokens or 100
        reply.completion_tokens = reply.completion_tokens or 20
        return reply


def json_reply(obj: dict[str, Any]) -> LLMReply:
    return LLMReply(content=json.dumps(obj, ensure_ascii=False))


def tool_reply(name: str, args: dict[str, Any], call_id: str = "c1") -> LLMReply:
    return LLMReply(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


class FakeWeb:
    """可控的"网站"：测试中可以改变页面格式，模拟环境变化"""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {}
        self.requests: list[str] = []

    def tool(self):
        @tool(
            "fetch_page",
            description="获取网页 HTML",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        )
        def fetch_page(url: str) -> dict[str, Any]:
            self.requests.append(url)
            if url not in self.pages:
                raise ToolError(f"404: {url}")
            return {"status": 200, "body": self.pages[url]}

        return fetch_page


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def fast() -> FakeLLM:
    return FakeLLM("fast")


@pytest.fixture
def strong() -> FakeLLM:
    return FakeLLM("strong")


@pytest.fixture
def agent(tmp_path, fast, strong, web) -> Agent:
    config = AgentConfig(home=tmp_path / ".no-agent", distill_min_traces=2, shadow_min_runs=0)  # 影子运行另有专门测试
    return Agent(config, tools=[web.tool()], include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong))
