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


# ── 指南升级：参数 / 权限在转正时才应用 ──

URL = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
GUIDE_MD = """---
name: page-body
description: 获取网页正文
---

# 获取网页正文

用 fetch_page 打开网址。
"""


def _install_guide(agent: Agent, tmp_path) -> None:
    d = tmp_path / "dl" / "page-body"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(GUIDE_MD, encoding="utf-8")
    agent.install_skill(d)


def test_upgrade_candidate_does_not_change_skill_until_promoted(tmp_path, fast, strong, web):
    from no_agent.skills.models import Permissions

    agent = _make_agent(tmp_path, fast, strong, web)
    _install_guide(agent, tmp_path)
    store = agent.store
    perms = Permissions(tools=["fetch_page"])

    s = store.add_version("page-body", SCRIPT, reason="升级", pass_rate=1.0, activate=False, params_schema=URL, permissions=perms)
    assert s.kind == "guide" and s.shadow_version == 1
    assert s.params_schema.get("required") is None and s.permissions.tools == []  # 技能本身没变
    assert s.params_schema_for(1) == URL and s.permissions_for(1).tools == ["fetch_page"]  # 候选带着自己的

    s = store.reject_shadow("page-body", "不一致")  # 被淘汰：无需回滚
    assert s.kind == "guide" and s.params_schema.get("required") is None and s.permissions.tools == []

    store.add_version("page-body", SCRIPT, reason="再升级", pass_rate=1.0, activate=False, params_schema=URL, permissions=perms)
    s = store.promote("page-body")  # 转正：这时才应用
    assert s.kind == "flow" and s.params_schema == URL and s.permissions.tools == ["fetch_page"]
    store.reload()  # 落盘后也一样
    s = store.get("page-body")
    assert s.params_schema == URL and s.permissions.tools == ["fetch_page"]


def test_rejected_upgrade_review_leaves_skill_untouched(tmp_path, fast, strong, web):
    from no_agent.skills.models import Permissions, Review

    agent = _make_agent(tmp_path, fast, strong, web)
    _install_guide(agent, tmp_path)
    review = Review(kind="upgrade", reasons=["新增工具: fetch_page"])
    agent.store.add_version(
        "page-body", SCRIPT, reason="升级", pass_rate=1.0, activate=False, review=review,
        params_schema=URL, permissions=Permissions(tools=["fetch_page"]),
    )
    s = agent.store.reject_review("page-body", 1, "不需要")
    assert s.kind == "guide" and s.params_schema.get("required") is None and s.permissions.tools == []


# ── 下线与候选版本 ──


def _flow_with_candidate(agent: Agent) -> Skill:
    _create_skill(agent)
    return agent.store.add_version("fetch-body", SCRIPT, reason="修补", pass_rate=1.0, activate=False)


def _fill_failures(agent: Agent, skill: Skill) -> None:
    skill.stats.recent = [False] * agent.config.retire_window


def test_no_auto_retire_while_candidate_in_shadow(tmp_path, fast, strong, web):
    from no_agent.skills.matcher import MatchResult

    agent = _make_agent(tmp_path, fast, strong, web)
    skill = _flow_with_candidate(agent)
    _fill_failures(agent, skill)
    agent.engine._record_skill(MatchResult(skill=skill, params={}, confidence=1), None, True, 1)
    assert agent.store.get("fetch-body").status == "active"  # 候选还在影子运行：先不下线

    agent.store.reject_shadow("fetch-body", "不一致")
    skill = agent.store.get("fetch-body")
    _fill_failures(agent, skill)
    agent.engine._record_skill(MatchResult(skill=skill, params={}, confidence=1), None, True, 1)
    assert agent.store.get("fetch-body").status == "retired"  # 没有候选了：照常下线


def test_retired_skill_with_candidate_still_gets_shadow_runs(tmp_path, fast, strong, web):
    agent = _make_agent(tmp_path, fast, strong, web)
    skill = _flow_with_candidate(agent)
    skill.status = "retired"
    agent.store.save(skill)
    s = agent.store.get("fetch-body")
    assert s.awaiting_shadow_match  # 不会被匹配，但候选仍会在同类任务上影子运行，通过后重新上线
    assert agent.store.promote("fetch-body").status == "active"


def test_manual_retire_drops_candidate(tmp_path, fast, strong, web):
    agent = _make_agent(tmp_path, fast, strong, web)
    _flow_with_candidate(agent)
    s = agent.store.set_status("fetch-body", "retired")
    assert s.shadow is None and not s.awaiting_shadow_match
    assert s.version_info(2).status == "rejected" and "手动下线" in s.version_info(2).note


# ── 主流程的异常处理 ──


async def test_worker_start_failure_degrades_to_freeform(tmp_path, fast, strong, web):
    from no_agent.skills.process import WorkerStartError

    class BrokenRunner:
        async def open(self, *a, **k):
            raise WorkerStartError("无法启动脚本 worker: 测试")

    web.pages["http://a.test/"] = "hello"
    strong.on("freeform", _fetch_then(lambda m: tool_reply("finish", {"success": True, "result": {"body": "hello"}})))
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        _create_skill(agent)
        agent.engine.runner = BrokenRunner()
        run = await agent.skill("fetch-body").run({"url": "http://a.test/"})
        assert run.success and run.mode == "freeform" and "测试" in run.trace["execution_error"]
        assert "takeover" not in strong.calls  # 不是脚本的错：不接管
        assert agent.store.get("fetch-body").stats.runs == 0  # 不计入技能统计
        assert agent.evolver.queue.qsize() == 0 and len(agent.evolver.history) == 0  # 不修补、不提炼


async def test_unexpected_error_is_recorded_and_reraised(tmp_path, fast, strong, web):
    def boom(messages, tools):
        raise RuntimeError("LLM 服务不可用")

    strong.on("freeform", boom)
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        with pytest.raises(RuntimeError, match="LLM 服务不可用"):
            await agent.run("随便做点什么")
        [run] = agent.runs.list()
        assert run.mode == "error" and not run.success and "LLM 服务不可用" in run.error
        assert agent.evolver.queue.qsize() == 0


# ── HTTP 鉴权 ──


def test_http_requires_token_when_configured(tmp_path, fast, strong, web):
    from fastapi.testclient import TestClient

    from no_agent.server import create_app

    agent = _make_agent(tmp_path, fast, strong, web)
    with TestClient(create_app(agent, api_token="s3cret")) as client:
        assert client.get("/health").status_code == 200  # 健康检查不需要鉴权
        assert client.get("/api/v1/tools").status_code == 401
        assert client.get("/api/v1/tools", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/v1/tools", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_http_without_token_only_accepts_local(tmp_path, fast, strong, web, monkeypatch):
    from fastapi.testclient import TestClient

    from no_agent.server import create_app

    monkeypatch.delenv("NO_AGENT_API_TOKEN", raising=False)
    agent = _make_agent(tmp_path, fast, strong, web)
    with TestClient(create_app(agent), client=("10.0.0.8", 5000)) as client:
        r = client.get("/api/v1/tools")
        assert r.status_code == 403 and "NO_AGENT_API_TOKEN" in r.json()["detail"]
    with TestClient(create_app(agent), client=("::1", 5000)) as client:
        assert client.get("/api/v1/tools").status_code == 200


def test_router_token(tmp_path, fast, strong, web):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from no_agent.server import create_router

    app = FastAPI()
    app.include_router(create_router(_make_agent(tmp_path, fast, strong, web), api_token="t"), prefix="/x")
    with TestClient(app) as client:
        assert client.get("/x/tools").status_code == 401
        assert client.get("/x/tools", headers={"Authorization": "bearer t"}).status_code == 200


# ── 进化器不写回旧对象 ──


def test_record_shadow_uses_fresh_skill(tmp_path, fast, strong, web):
    from no_agent.storage.sql import Run

    agent = _make_agent(tmp_path, fast, strong, web)
    agent.config.shadow_min_runs = 3  # 一次通过不够转正，才能看到记录落在哪个对象上
    stale = _flow_with_candidate(agent)
    agent.store.reload()  # 影子运行期间技能被重新加载（例如 install / sync）
    run = Run(task="t", mode="skill", success=True)

    out = agent.evolver._record_shadow(stale, run, True, "一致")
    assert out["status"] == "shadow_pass"
    assert agent.store.get("fetch-body").shadow.passes == 1  # 记在最新的对象上

    agent.store.reject_shadow("fetch-body", "x")
    agent.store.add_version("fetch-body", SCRIPT, reason="新候选", pass_rate=1.0, activate=False)
    out = agent.evolver._record_shadow(stale, run, True, "一致")  # stale 还指着旧候选 v2
    assert out["status"] == "skipped"
    assert agent.store.get("fetch-body").shadow.passes == 0


# ── 变更审查：动态调用按内容比较 ──


DYN_BASE = """
@step
async def a(ctx):
    name = ctx.params["t"]
    await ctx.tool(name, x=1)
"""

# 数量没变，但换成了另一个动态目标
DYN_SWAPPED = """
@step
async def a(ctx):
    other = ctx.state["t"]
    await ctx.tool(other, x=1)
"""

# 只是挪了行号、改了其他参数
DYN_MOVED = """
@step
async def a(ctx):
    ctx.log("x")
    name = ctx.params["t"]
    await ctx.tool(name, x=2)
"""

# 同一个动态调用多写了一次
DYN_TWICE = """
@step
async def a(ctx):
    name = ctx.params["t"]
    await ctx.tool(name, x=1)
    await ctx.tool(name, x=1)
"""


def test_review_flags_replaced_dynamic_call():
    from no_agent.skills.review import review_patch

    skill = Skill(id="s", name="s", description="d")
    r = review_patch(skill, 1, DYN_BASE, DYN_SWAPPED)  # 换了目标 → 需要审查
    assert r is not None and any("ctx.tool" in x for x in r.dynamic_calls)

    assert review_patch(skill, 1, DYN_BASE, DYN_MOVED) is None  # 不算新的动态调用

    r = review_patch(skill, 1, DYN_BASE, DYN_TWICE)  # 按次数算 → 需要审查
    assert r is not None and len(r.dynamic_calls) == 1


# ── 回放：同样参数录制了多次 ──


async def test_replay_repeated_calls_in_recorded_order():
    @tool("poll")
    def poll(job: str):
        raise AssertionError("不应真实执行")

    fixtures = [{"tool": "poll", "args": {"job": "j"}, "ok": True, "output": s} for s in ("running", "running", "done")]
    box = ReplayToolbox(ToolRegistry([poll]), fixtures)
    got = [await box.call("poll", {"job": "j"}) for _ in range(4)]
    assert got == ["running", "running", "done", "done"]  # 按录制顺序，用完后一直返回最后一次


# ── 反馈：库 / HTTP / MCP 共用 Agent.feedback ──


async def test_agent_feedback_queues_patch(tmp_path, fast, strong, web):
    web.pages["http://a.test/"] = "hello"
    async with _make_agent(tmp_path, fast, strong, web) as agent:
        _create_skill(agent)
        run = await agent.skill("fetch-body").run({"url": "http://a.test/"})
        assert run.mode == "skill"
        agent.evolver.handle = _noop_handle  # type: ignore[method-assign]  # 只看是否投递了修补

        out = agent.feedback(run.id, -1, "正文不对")
        assert out == {"ok": True, "patch_queued": True}
        saved = agent.runs.get(run.id)
        assert saved.feedback_score == -1 and saved.feedback_comment == "正文不对"
        assert agent.store.get("fetch-body").stats.feedback_count == 1

        assert agent.feedback(run.id, 1)["patch_queued"] is False
        with pytest.raises(KeyError):
            agent.feedback("nope", 1)
        with pytest.raises(ValueError):
            agent.feedback(run.id, 5)
        await agent.wait_idle()
        assert [h["kind"] for h in agent.evolver.history] == ["feedback"]


async def _noop_handle(job):
    return {"status": "ok"}
