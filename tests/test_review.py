"""P4：扩大能力的新版本 / 新技能不自动上线，等人批准"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from no_agent import Skill, tool
from no_agent.server.routes import create_router
from no_agent.skills.compose import SkillCaller
from no_agent.skills.models import Permissions
from no_agent.skills.review import capabilities, review_new, review_patch
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import Toolbox
from tests.conftest import json_reply, tool_reply
from tests.test_evolution import EXTRACT_V2, SKILL_V1, _url, setup_freeform
from tests.test_script import NoLLM

URL = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}


# ── 静态能力分析 ──


def test_capabilities_literal_and_dynamic_calls():
    src = '''
import re
from urllib.parse import quote

@step
async def a(ctx):
    await ctx.tool("fetch_page", url="x")
    await ctx.skill("login", user="u")
    await ctx.ask([])
    name = ctx.params["tool"]
    await ctx.tool(name)
    t = ctx.tool
    await t("hidden")
    f = ctx.llm
    return await ctx.llm("hi")
'''
    caps = capabilities(src)
    assert caps.tools == {"fetch_page", "ask_user"} and caps.skills == {"login"}
    assert caps.imports == {"re", "urllib.parse"} and caps.llm_calls == 2  # 赋给变量也算一个调用点
    assert len(caps.dynamic) == 2 and "第 11 行" in caps.dynamic[0] and "第 12 行" in caps.dynamic[1]


def _skill(**perms) -> Skill:
    return Skill(id="s", name="s", description="s", permissions=Permissions(**perms))


BASE = '@step\nasync def a(ctx):\n    return await ctx.tool("fetch_page")\n'


@pytest.mark.parametrize(
    "new,perms,reasons",
    [
        (BASE.replace("return", "import json\n    x = await ctx.llm('q')\n    return"), {}, None),  # 只改逻辑 / import / ctx.llm
        (BASE + '\n@step\nasync def b(ctx):\n    return await ctx.tool("send_mail")\n', {}, ["新增工具: send_mail"]),
        (BASE + '\n@step\nasync def b(ctx):\n    return await ctx.tool("send_mail")\n', {"tools": ["fetch_page", "send_mail"]}, None),
        (BASE + '\n@step\nasync def b(ctx):\n    return await ctx.skill("pay")\n', {}, ["新增子技能调用: pay"]),
        (BASE + '\n@step\nasync def b(ctx):\n    return await ctx.tool(ctx.params["t"])\n', {}, ["第 7 行: ctx.tool 的目标不是字面量，无法静态确定会调用什么"]),
    ],
)
def test_review_patch(new, perms, reasons):
    review = review_patch(_skill(**perms), 1, BASE, new)
    assert (review.reasons if review else None) == reasons


def test_review_patch_records_but_does_not_trigger_on_imports_and_llm():
    new = "import json\n" + BASE.replace("return", "await ctx.llm('q')\n    await ctx.tool('send_mail')\n    return")
    review = review_patch(_skill(), 1, BASE, new)
    assert review.added_tools == ["send_mail"] and review.added_imports == ["json"]
    assert (review.llm_calls_before, review.llm_calls_after) == (0, 1)


def test_review_new_uses_trace_tools_and_checks_atoms():
    assert review_new("distill", BASE, trace_tools={"fetch_page"}) is None
    atom = '@step\nasync def a(ctx):\n    return await ctx.tool("delete_all")\n'
    review = review_new("distill", BASE, trace_tools={"fetch_page"}, atoms={"cleaner": atom})
    assert review.reasons == ["新增工具: delete_all"] and review.atoms == ["cleaner"]
    # 指南升级：技能已声明工具时，以声明为准
    assert review_new("upgrade", BASE, trace_tools=set(), declared_tools=["fetch_page"]) is None


# ── 存储：待审 → 批准 / 拒绝 ──


def test_pending_version_does_not_touch_active_or_candidate(agent):
    store = agent.store
    store.create(Skill(id="s", name="s", description="s"), BASE, reason="v1")
    store.add_version("s", BASE.replace("fetch_page", "fetch_page2"), reason="v2", pass_rate=1, activate=False)  # 影子候选
    review = review_patch(store.get("s"), 2, BASE, BASE + '\n@step\nasync def b(ctx):\n    await ctx.tool("x")\n')
    s = store.add_version("s", BASE, reason="v3", pass_rate=1, review=review)
    assert (s.active_version, s.shadow_version, s.pending_review.version) == (1, 2, 3)

    s = store.approve_review("s", 3, shadow=True, comment="看过了")
    assert (s.active_version, s.shadow_version, s.pending_review) == (1, 3, None)
    assert [v.status for v in s.versions] == ["active", "rejected", "candidate"]
    assert s.versions[2].review.decision == "approved" and s.versions[2].review.comment == "看过了"
    with pytest.raises(ValueError, match="没有在等待审查"):
        store.approve_review("s", 3, shadow=True)


async def test_pending_new_skill_is_invisible_until_approved(agent, web):
    store = agent.store
    atom_review = review_new("distill", BASE, trace_tools=set())
    atom_review.parent = "parent"
    store.create(Skill(id="atom", name="a", description="a", role="atomic"), BASE, reason="t", review=atom_review)
    parent_src = '@step\nasync def a(ctx):\n    return await ctx.skill("atom")\n'
    review = review_new("distill", parent_src, trace_tools=set(), atoms={"atom": BASE})
    store.create(Skill(id="parent", name="p", description="p", params_schema=URL), parent_src, reason="t", review=review)

    assert store.get("parent").status == "pending_review" and agent.skills() == []
    with pytest.raises(PermissionError, match="等待审查"):
        await agent.skill("parent").run({"url": "http://site/1"})
    res = await execute_script(
        parent_src, task="t", params={}, toolbox=Toolbox(agent.registry), llm=NoLLM(), skills=SkillCaller(store, NoLLM())
    )
    assert "atom 还在等待审查，不能被调用" in res.error
    with pytest.raises(ValueError, match="请审查 parent"):
        store.approve_review("atom", 1, shadow=False)  # 原子技能不能单独批准

    [r] = agent.reviews()
    assert r["skill_id"] == "parent" and r["review"]["atoms"] == ["atom"] and "ctx.skill" in r["diff"]
    agent.approve("parent", 1)
    assert store.get("parent").status == "active" and store.get("atom").status == "active"


def test_rejecting_new_skill_retires_atoms(agent):
    store = agent.store
    atom_review = review_new("distill", BASE, trace_tools=set())
    atom_review.parent = "p"
    store.create(Skill(id="a", name="a", description="a", role="atomic"), BASE, reason="t", review=atom_review)
    review = review_new("distill", BASE, trace_tools=set(), atoms={"a": BASE})
    store.create(Skill(id="p", name="p", description="p"), BASE, reason="t", review=review)
    s = agent.reject("p", 1, "不需要发邮件")
    assert s.status == "retired" and store.get("a").status == "retired"
    assert s.versions[0].status == "rejected" and s.versions[0].note == "不需要发邮件"


# ── 进化器端到端 ──


def _meta_tool():
    @tool("page_meta", description="读取网页元信息", parameters={"type": "object", "properties": {"url": {"type": "string"}}})
    def page_meta(url: str = "") -> dict:
        return {"lang": "zh"}

    return page_meta


async def _patch_once(agent, fast, strong, web, functions):
    """一个没有声明权限的技能：v1 在大写 TITLE 的页面上失败 → AI 接管 → 修补"""
    agent.add_tool(_meta_tool())
    agent.store.create(Skill(id="page-title", name="t", description="获取网页标题", params_schema=URL), SKILL_V1, reason="t")
    web.pages = {"http://site/3": "<TITLE> C </TITLE>"}
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.9, "params": {"url": _url(m)}}))
    strong.on("takeover", lambda m, t: tool_reply("finish", {"success": True, "result": {"title": "C"}}))
    strong.on("patch", lambda m, t: json_reply({"mode": "functions", "functions": functions, "reason": "兼容大写"}))
    agent.evolver.start()
    await agent.run("获取 http://site/3 的网页标题")
    await agent.evolver.drain()
    await agent.evolver.stop()
    return agent.evolver.history[-1]


async def test_logic_only_patch_is_promoted_automatically(agent, fast, strong, web):
    out = await _patch_once(agent, fast, strong, web, {"extract": EXTRACT_V2})
    assert out["status"] == "promoted" and agent.store.get("page-title").active_version == 2


async def test_patch_adding_a_tool_waits_for_review(agent, fast, strong, web):
    extract = EXTRACT_V2.replace('    return {"title"', '    await ctx.tool("page_meta", url=ctx.params["url"])\n    return {"title"')
    out = await _patch_once(agent, fast, strong, web, {"extract": extract})
    assert out["status"] == "pending_review" and out["review"] == ["新增工具: page_meta"]
    s = agent.store.get("page-title")
    assert s.active_version == 1 and s.pending_review.version == 2

    app = FastAPI()
    app.include_router(create_router(agent), prefix="/api/v1")  # 审批接口不需要启动 agent
    with TestClient(app) as client:
        [r] = client.get("/api/v1/reviews").json()
        assert r["skill_id"] == "page-title" and '+    await ctx.tool("page_meta"' in r["diff"]
        assert client.post("/api/v1/skills/page-title/versions/2/approve", json={"comment": "ok"}).json()["active_version"] == 2
        assert client.post("/api/v1/skills/page-title/versions/2/approve", json={}).status_code == 409


async def test_distill_using_unseen_tool_waits_for_review(agent, fast, strong, web):
    agent.add_tool(_meta_tool())
    web.pages = {"http://site/1": "<title>A</title>", "http://site/2": "<title>B</title>"}
    setup_freeform(strong)
    script = SKILL_V1.replace('    ctx.state["html"] = r["body"]', '    ctx.state["html"] = r["body"]\n    await ctx.tool("page_meta")')
    strong.on("distill", lambda m, t: json_reply({
        "use": [1, 2], "id": "page-title", "name": "t", "description": "获取网页标题", "params_schema": URL,
        "script": script, "test_params": [{"url": "http://site/1"}, {"url": "http://site/2"}],
    }))
    agent.evolver.start()
    await agent.run("获取 http://site/1 的网页标题")
    await agent.run("获取 http://site/2 的网页标题")
    await agent.evolver.drain()
    await agent.evolver.stop()
    statuses = [h["status"] for h in agent.evolver.history]
    out = next(h for h in agent.evolver.history if h["status"] == "pending_review")
    assert out["review"] == ["新增工具: page_meta"]  # 轨迹里只用过 fetch_page
    assert statuses.count("pending_review") == 1, statuses  # 待审的技能挡住了重复提炼
    assert agent.store.get("page-title").status == "pending_review" and agent.skills() == []
