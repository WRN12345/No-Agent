"""
阶段 2：前置检查（guard）、影子运行 + 自动转正、权限强制检查

影子运行完整流程：
  修补 / 提炼出的新版本 → 通过回归测试 → 成为候选（不对外生效）
  → 每次真实运行后，后台用录制数据跑候选，与真实结果比对 → 连续 N 次一致自动转正；失败过多淘汰
"""

import pytest

from no_agent import Agent, AgentConfig, Skill
from no_agent.llm import LLMs
from no_agent.skills.models import Permissions
from no_agent.tools.base import PermissionDenied, Toolbox, ToolRegistry, tool
from tests.conftest import json_reply, tool_reply
from tests.test_evolution import EXTRACT_V2, SKILL_V1, _url, setup_freeform

URL_SCHEMA = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}


def make_agent(tmp_path, fast, strong, web, **cfg):
    config = AgentConfig(home=tmp_path / ".no-agent", distill_min_traces=2, **{"shadow_min_runs": 2, **cfg})
    return Agent(config, tools=[web.tool()], include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=None)


def add_page_title(agent, script=SKILL_V1, **kw):
    return agent.store.create(
        Skill(id="page-title", name="网页标题", description="获取网页标题", params_schema=URL_SCHEMA, **kw), script, reason="t"
    )


def match_page_title(fast):
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.95, "params": {"url": _url(m)}}))


def takeover_returns_title(strong):
    import re

    def takeover(messages, tools):
        url = re.search(r"http://\S+", messages[1]["content"]).group(0)
        return tool_reply("finish", {"success": True, "result": {"title": f"Page {url.rsplit('/', 1)[-1]}"}})

    strong.on("takeover", takeover)


# ── 影子运行：修补出的候选版本 ──


async def test_patched_version_shadows_then_auto_promotes(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web)
    add_page_title(agent)
    match_page_title(fast)
    takeover_returns_title(strong)
    strong.on("patch", lambda m, t: json_reply({"mode": "functions", "functions": {"extract": EXTRACT_V2}, "reason": "大小写"}))
    web.pages = {f"http://site/{i}": f"<TITLE>Page {i}</TITLE>" for i in range(1, 6)}  # 网站改版

    async with agent:
        # 第 1 次：v1 失败 → 接管 → 修补出 v2，但只是候选，不上线
        r1 = await agent.run("获取 http://site/1 的标题")
        await agent.wait_idle()
        assert r1.mode == "skill_takeover" and r1.success
        assert agent.evolver.history[-1]["status"] == "candidate"
        skill = agent.store.get("page-title")
        assert skill.active_version == 1 and skill.shadow_version == 2
        assert [v.status for v in skill.versions] == ["active", "candidate"]

        # 第 2 次：真实运行仍走 v1 + 接管（可靠优先）；后台影子跑 v2，结果一致 → 通过 1 次
        r2 = await agent.run("获取 http://site/2 的标题")
        await agent.wait_idle()
        assert r2.mode == "skill_takeover" and r2.skill_version == 1
        kinds = [(h["kind"], h["status"]) for h in list(agent.evolver.history)[-2:]]
        assert kinds == [("shadow", "shadow_pass"), ("patch", "skipped")]  # 候选已能处理，不重复修补
        assert agent.store.get("page-title").shadow.passes == 1

        # 第 3 次：影子再通过 → 自动转正
        await agent.run("获取 http://site/3 的标题")
        await agent.wait_idle()
        assert ("shadow", "promoted") in [(h["kind"], h["status"]) for h in agent.evolver.history]
        skill = agent.store.get("page-title")
        assert skill.active_version == 2 and skill.shadow is None
        assert [v.status for v in skill.versions] == ["superseded", "active"]

        # 第 4 次：v2 直接完成，不再需要接管
        strong_calls = len(strong.calls)
        r4 = await agent.run("获取 http://site/4 的标题")
        assert r4.mode == "skill" and r4.skill_version == 2 and r4.result == {"title": "Page 4"}
        assert len(strong.calls) == strong_calls


async def test_shadow_disagreement_rejects_candidate(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web, shadow_max_failures=0)
    add_page_title(agent)
    match_page_title(fast)
    takeover_returns_title(strong)
    wrong = EXTRACT_V2.replace("m.group(1).strip()", "m.group(1).strip().upper()")  # 看似修好，实际结果不对
    prompts = []

    def patch(messages, tools):
        prompts.append(messages[1]["content"])
        return json_reply({"mode": "functions", "functions": {"extract": wrong if len(prompts) == 1 else EXTRACT_V2}})

    strong.on("patch", patch)
    fast.on("shadow_judge", lambda m, t: json_reply({"same": False, "reason": "标题大小写不同"}))
    web.pages = {f"http://site/{i}": f"<TITLE>Page {i}</TITLE>" for i in range(1, 4)}

    async with agent:
        await agent.run("获取 http://site/1 的标题")
        await agent.wait_idle()
        assert agent.store.get("page-title").shadow_version == 2
        # 注意：修补时的回归测试抓不到这个错误（verify 只检查非空），影子运行抓到了
        await agent.run("获取 http://site/2 的标题")
        await agent.wait_idle()
        shadow_jobs = [h for h in agent.evolver.history if h["kind"] == "shadow"]
        assert shadow_jobs[-1]["status"] == "shadow_rejected" and "大小写" in shadow_jobs[-1]["reason"]
        skill = agent.store.get("page-title")
        assert skill.active_version == 1  # 错误的候选从未对外生效
        assert skill.versions[1].status == "rejected" and "大小写" in skill.versions[1].note
        # 同一次运行仍是接管 → 重新修补；新的修补知道上一个候选为什么被淘汰
        assert skill.shadow_version == 3
        assert "之前被淘汰的修补" in prompts[1] and "大小写" in prompts[1]


# ── 影子运行：新提炼的技能 ──


async def test_distilled_skill_shadows_before_going_live(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web)
    setup_freeform(strong)
    match_page_title(fast)
    web.pages = {f"http://site/{i}": f"<title>Page {i}</title>" for i in range(1, 7)}
    strong.on(
        "distill",
        lambda m, t: json_reply(
            {
                "use": [1, 2], "id": "page-title", "name": "网页标题", "description": "获取网页标题",
                "params_schema": URL_SCHEMA, "script": SKILL_V1,
                "test_params": [{"url": "http://site/2"}, {"url": "http://site/1"}],
            }
        ),
    )
    async with agent:
        for i in (1, 2):
            await agent.run(f"获取 http://site/{i} 的网页标题")
        await agent.wait_idle()
        skill = agent.store.get("page-title")
        assert skill.status == "shadow" and skill.permissions.tools == ["fetch_page"]  # 自动生成权限声明
        assert agent.skills() == []  # 影子期不对外生效

        # 第 3、4 次仍由 AI 完成；后台影子跑新技能并比对
        for i in (3, 4):
            r = await agent.run(f"获取 http://site/{i} 的网页标题")
            assert r.mode == "freeform"
        await agent.wait_idle()
        statuses = [h["status"] for h in agent.evolver.history if h["kind"] == "shadow_match"]
        assert statuses == ["shadow_pass", "promoted"]
        assert agent.store.get("page-title").status == "active"
        assert [h["status"] for h in agent.evolver.history if h["kind"] == "distill"][-2:] == ["skipped", "skipped"]  # 不会重复提炼

        r5 = await agent.run("获取 http://site/5 的网页标题")
        assert r5.mode == "skill" and r5.result == {"title": "Page 5"}


# ── 前置检查 ──

GUARDED = SKILL_V1 + '''

async def guard(ctx):
    if not ctx.params["url"].startswith("http://site/"):
        return "只适用于 site 站点"
    return True
'''


async def test_guard_rejection_runs_no_steps(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web)
    add_page_title(agent, GUARDED)
    match_page_title(fast)
    strong.on("takeover", lambda m, t: tool_reply("finish", {"success": True, "result": {"title": "Other"}}))
    strong.on("patch", lambda m, t: json_reply({"give_up": True, "reason": "其他站点不归这个技能管"}))
    web.pages = {"http://other/1": "<title>Other</title>"}
    async with agent:
        run = await agent.run("获取 http://other/1 的标题")
        await agent.wait_idle()
    exe = run.trace["execution"]
    assert exe["guard_rejected"] and exe["steps"] == [] and "只适用于 site 站点" in exe["error"]
    assert run.mode == "skill_takeover" and run.success
    assert web.requests == []  # 脚本一步都没执行


async def test_guard_cannot_call_side_effect_tools(tmp_path, fast, strong, web):
    from no_agent.skills.runtime import execute_script
    from tests.test_script import NoLLM

    @tool("delete_all", side_effect=True)
    def delete_all():
        raise AssertionError("不应执行")

    src = SKILL_V1 + '\n\nasync def guard(ctx):\n    await ctx.tool("delete_all")\n    return True\n'
    res = await execute_script(src, task="t", params={"url": "x"}, toolbox=Toolbox(ToolRegistry([delete_all])), llm=NoLLM())
    assert res.guard_rejected and "只能调用只读工具" in res.error


# ── 权限 ──


async def test_runtime_permission_enforced_then_ai_takes_over(tmp_path, fast, strong, web):
    hits = []

    @tool("read_secret", description="读取敏感数据")
    def read_secret():
        hits.append(1)
        return "secret"

    sneaky = SKILL_V1.replace('ctx.state["html"] = r["body"]', 'ctx.state["html"] = r["body"]\n    await ctx.tool("read_secret")')
    agent = make_agent(tmp_path, fast, strong, web)
    agent.add_tool(read_secret)
    add_page_title(agent, sneaky, permissions=Permissions(tools=["fetch_page"]))
    match_page_title(fast)
    strong.on("takeover", lambda m, t: tool_reply("finish", {"success": True, "result": {"title": "Page 1"}}))
    # 修补试图继续用未声明的工具 → 被打回，权限只能由人改
    strong.on("patch", lambda m, t: json_reply({"mode": "script", "script": sneaky}))
    web.pages = {"http://site/1": "<title>Page 1</title>"}
    async with agent:
        run = await agent.run("获取 http://site/1 的标题")
        await agent.wait_idle()
    assert hits == []  # 未声明的工具没有被执行
    assert run.mode == "skill_takeover" and "未声明使用工具 read_secret" in run.trace["execution"]["error"]
    patch = [h for h in agent.evolver.history if h["kind"] == "patch"][-1]
    assert patch["status"] == "rejected" and "未声明的工具 read_secret" in patch["detail"]


def test_domain_permission():
    @tool("open", parameters={"type": "object", "properties": {"url": {"type": "string"}}}, url_arg="url")
    def open_(url):
        return url

    box = Toolbox(ToolRegistry([open_]))
    box.restrict(["open"], ["example.com", "*.corp.cn"])
    box.check_permission(open_, {"url": "https://example.com/a"})
    box.check_permission(open_, {"url": "https://www.example.com/a"})
    box.check_permission(open_, {"url": "https://oa.corp.cn/x"})
    for bad in ("https://evil.com", "https://example.com.evil.com", "https://corp.cn/"):
        with pytest.raises(PermissionDenied):
            box.check_permission(open_, {"url": bad})
    box.restrict(None, None)
    box.check_permission(open_, {"url": "https://anything.org"})


def test_install_rejects_scripts_using_undeclared_tools(agent, tmp_path):
    import json

    d = tmp_path / "x-skill"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x-skill\ndescription: x\n---\nbody", encoding="utf-8")
    (d / "no-agent.json").write_text(
        json.dumps({"name": "x", "active_version": 1, "versions": [{"version": 1}], "permissions": {"tools": ["fetch_page"]}}),
        encoding="utf-8",
    )
    (d / "scripts" / "flow_v1.py").write_text('@step\nasync def a(ctx):\n    await ctx.tool("write_file", path="a", content="b")\n', encoding="utf-8")
    with pytest.raises(ValueError, match="未声明的工具: write_file"):
        agent.install_skill(d)


def test_api_shadow_status_and_manual_promote(tmp_path, fast, strong, web):
    from fastapi.testclient import TestClient

    from no_agent.server import create_app

    agent = make_agent(tmp_path, fast, strong, web)
    add_page_title(agent)
    agent.store.add_version("page-title", SKILL_V1 + "\n", reason="候选", pass_rate=1.0, activate=False)
    with TestClient(create_app(agent), client=("127.0.0.1", 50000)) as client:
        s = client.get("/api/v1/skills").json()[0]
        assert (s["active_version"], s["shadow_version"], s["shadow_passes"]) == (1, 2, 0)
        assert client.post("/api/v1/skills/page-title/promote").json()["active_version"] == 2
        assert client.post("/api/v1/skills/page-title/promote").status_code == 409  # 已经没有候选
