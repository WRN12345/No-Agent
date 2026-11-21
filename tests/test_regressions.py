"""
已修复问题的回归测试：
  - 同一个 Agent 多次 run_sync（每次一个新的事件循环），后台进化任务照常处理
  - stop(wait=False) 取消执行中的进化任务后，drain() 不会永远挂起
  - 显式调用技能、参数抽取失败时报错，而不是带着空参数开跑
  - 回放时依赖实时状态的工具（stateful）不真实执行
  - 接管 / 用过工具后只回一段文字，不算完成
"""

import asyncio

import pytest

from no_agent import Agent, AgentConfig
from no_agent.engine.evolver import Job
from no_agent.llm import LLMReply, LLMs
from no_agent.skills.models import Skill
from no_agent.tools.base import ReplayToolbox, ToolError, ToolRegistry, tool
from tests.conftest import json_reply, tool_reply

SCRIPT = '''
@step
async def fetch(ctx):
    r = await ctx.tool("fetch_page", url=ctx.params["url"])
    return {"body": r["body"]}
'''


def _make_agent(tmp_path, fast, strong, web) -> Agent:
    config = AgentConfig(home=tmp_path / ".no-agent", distill_min_traces=2, shadow_min_runs=0)
    return Agent(config, tools=[web.tool()], include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=None)


def _fetch_then(final):
    """先抓页面，下一轮回复 final(messages)"""

    def handler(messages, tools):
        if messages[-1]["role"] == "user" and len(messages) == 2:
            return tool_reply("fetch_page", {"url": "http://a.test/"})
        return final(messages)

    return handler


# ── 事件循环 ──


def test_run_sync_twice_on_same_agent(tmp_path, fast, strong, web):
    web.pages["http://a.test/"] = "<title>A</title>"
    strong.on("freeform", _fetch_then(lambda m: tool_reply("finish", {"success": True, "result": "A"})))
    strong.on("distill", lambda m, t: json_reply({"skip": True, "reason": "测试"}))
    agent = _make_agent(tmp_path, fast, strong, web)

    for _ in range(2):
        run = agent.run_sync("抓一下 http://a.test/ 的标题")
        assert run.success and run.mode == "freeform"

    # 两次运行各投递了一个提炼任务，都在各自的事件循环里处理完了
    assert [h["kind"] for h in agent.evolver.history] == ["distill", "distill"]
    assert [h["status"] for h in agent.evolver.history] == ["waiting", "skipped"]


async def test_drain_after_cancelled_job_does_not_hang(tmp_path, fast, strong, web):
    agent = _make_agent(tmp_path, fast, strong, web)
    started = asyncio.Event()

    async def slow(job):
        started.set()
        await asyncio.sleep(30)
        return {"status": "ok"}

    agent.evolver.handle = slow  # type: ignore[method-assign]
    await agent.start()
    agent.evolver.submit(Job("distill", "x"))
    await asyncio.wait_for(started.wait(), 5)
    await agent.stop(wait=False)  # 取消执行中的任务

    await agent.start()
    await asyncio.wait_for(agent.wait_idle(), 2)
    assert agent.evolver.history[-1]["status"] == "cancelled"
    await agent.stop()


# ── 显式调用技能的参数抽取 ──


def _create_skill(agent: Agent) -> None:
    skill = Skill(
        id="fetch-body",
        name="抓取页面",
        description="抓取网页正文",
        params_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    agent.store.create(skill, SCRIPT, reason="测试")


async def test_explicit_skill_without_extractable_params_raises(tmp_path, fast, strong, web):
    fast.on("match", lambda m, t: json_reply({"skill_id": None, "confidence": 0, "params": {}, "reason": "没有网址"}))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        _create_skill(agent)
        with pytest.raises(ValueError, match="无法从任务中抽取"):
            await agent.skill("fetch-body").run(task="帮我抓一下那个页面")
        assert strong.calls == []  # 没有开跑，也就没有接管
        assert agent.runs.list() == []
        assert agent.evolver.queue.qsize() == 0  # 没有触发修补


async def test_explicit_skill_ignores_confidence_threshold(tmp_path, fast, strong, web):
    """用户已经指定了技能：置信度低于自动路由的阈值也照样使用抽出来的参数"""
    web.pages["http://a.test/"] = "hello"
    fast.on("match", lambda m, t: json_reply({"skill_id": "fetch-body", "confidence": 0.3, "params": {"url": "http://a.test/"}}))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        _create_skill(agent)
        run = await agent.skill("fetch-body").run(task="抓 http://a.test/")
        assert run.mode == "skill" and run.success and run.result == {"body": "hello"}


async def test_explicit_skill_rejects_invalid_extracted_params(tmp_path, fast, strong, web):
    fast.on("match", lambda m, t: json_reply({"skill_id": "fetch-body", "confidence": 0.9, "params": {"url": 123}}))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        _create_skill(agent)
        with pytest.raises(ValueError, match="参数不合法"):
            await agent.skill("fetch-body").run(task="抓 123")
        assert strong.calls == []


# ── 回放：stateful 工具 ──


async def test_replay_never_runs_stateful_tools_live():
    hits = []

    @tool("page_text", stateful=True)
    def page_text(sel: str):
        hits.append(sel)
        return "live"

    box = ReplayToolbox(ToolRegistry([page_text]), [{"tool": "page_text", "args": {"sel": "a"}, "ok": True, "output": "recorded"}])
    assert await box.call("page_text", {"sel": "a"}) == "recorded"
    with pytest.raises(ToolError, match="依赖实时状态"):
        await box.call("page_text", {"sel": "b"})
    assert hits == []


def test_stateful_tools_record_larger_fixtures():
    from no_agent.tools.base import FIXTURE_OUTPUT_LIMIT, ToolCallRecord

    big = "x" * (FIXTURE_OUTPUT_LIMIT + 10)
    assert ToolCallRecord(tool="t", args={}, ok=True, output=big).to_fixture() is None
    assert ToolCallRecord(tool="t", args={}, ok=True, output=big, stateful=True).to_fixture() is not None


def test_browser_tools_are_stateful():
    pytest.importorskip("playwright")
    from no_agent.config import BrowserConfig
    from no_agent.tools.browser import BrowserSession, make_browser_tools

    tools = make_browser_tools(BrowserSession(BrowserConfig()))
    assert tools and all(t.stateful for t in tools)


# ── 只回文字不算完成 ──


async def test_text_reply_after_tool_use_needs_finish(tmp_path, fast, strong, web):
    web.pages["http://a.test/"] = "<title>A</title>"
    strong.on("freeform", _fetch_then(lambda m: LLMReply(content="抱歉，我做不到。")))
    strong.on("distill", lambda m, t: json_reply({"skip": True, "reason": "测试"}))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        run = await agent.run("抓一下 http://a.test/")
        assert not run.success and "finish" in run.error
        assert strong.calls.count("freeform") == 3  # 工具调用 → 文字 → 提醒后仍是文字
        assert agent.evolver.queue.qsize() == 0  # 失败的运行不提炼


async def test_nudge_then_finish_succeeds(tmp_path, fast, strong, web):
    web.pages["http://a.test/"] = "<title>A</title>"

    def final(messages):
        if "finish" in messages[-1]["content"] and messages[-1]["role"] == "user":
            return tool_reply("finish", {"success": True, "result": "A"})
        return LLMReply(content="标题是 A")

    strong.on("freeform", _fetch_then(final))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        run = await agent.run("抓一下 http://a.test/")
        assert run.success and run.result == "A"
