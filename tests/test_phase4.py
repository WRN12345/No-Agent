"""阶段 4：技能组合与原子技能、多候选提炼、按热度升级（指南 → 脚本）、SKILL.md 正文同步"""

import re

import pytest

from no_agent import Agent, AgentConfig, Skill
from no_agent.llm import LLMs
from no_agent.skills.compose import SkillCaller
from no_agent.skills.models import Permissions
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import Toolbox
from tests.conftest import json_reply, tool_reply
from tests.test_evolution import SKILL_V1, _url, setup_freeform
from tests.test_script import NoLLM

URL = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

FETCH_ATOM = '''
@step
async def fetch(ctx):
    r = await ctx.tool("fetch_page", url=ctx.params["url"])
    return {"html": r["body"]}
'''

TITLE_VIA_ATOM = '''
import re

@step
async def get(ctx):
    page = await ctx.skill("fetch-html", url=ctx.params["url"])
    m = re.search(r"<title>(.*?)</title>", page["html"], re.I | re.S)
    if not m:
        ctx.fail("没有 title")
    return {"title": m.group(1).strip()}


def verify(ctx, result):
    return bool(result.get("title")) or "title 为空"
'''

TITLE_WITH_LLM = '''
@step
async def fetch(ctx):
    r = await ctx.tool("fetch_page", url=ctx.params["url"])
    ctx.state["html"] = r["body"]


@step(kind="llm")
async def extract(ctx):
    return await ctx.llm("提取网页标题，输出 {\\"title\\": ...}", ctx.state["html"], json=True)


def verify(ctx, result):
    return bool(result.get("title")) or "title 为空"
'''


def make_agent(tmp_path, fast, strong, web, **cfg):
    config = AgentConfig(home=tmp_path / ".no-agent", distill_min_traces=2, **{"shadow_min_runs": 0, **cfg})
    return Agent(config, tools=[web.tool()], include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=None)


# ── 技能组合 ──


async def test_composed_skills_run_under_their_own_permissions(agent, web):
    agent.store.create(
        Skill(id="fetch-html", name="取网页", description="获取网页 HTML", params_schema=URL, role="atomic",
              permissions=Permissions(tools=["fetch_page"])),
        FETCH_ATOM, reason="t",
    )
    agent.store.create(
        Skill(id="page-title", name="标题", description="获取网页标题", params_schema=URL,
              permissions=Permissions(tools=["ask_user"], skills=["fetch-html"])),  # 自己不能直接用 fetch_page
        TITLE_VIA_ATOM, reason="t",
    )
    web.pages = {"http://site/1": "<TITLE> Composed </TITLE>"}
    run = await agent.skill("page-title").run({"url": "http://site/1"})
    await agent.stop()
    assert run.mode == "skill" and run.result == {"title": "Composed"}
    assert run.trace["tool_calls"][0]["tool"] == "fetch_page"  # 子技能的工具调用同样被录制

    # 原子技能不参与整任务匹配
    assert [s.id for s, _ in await agent.matcher.candidates("获取网页 HTML 标题")] == ["page-title"]


async def test_undeclared_subskill_cycle_and_child_failure(agent, web):
    store = agent.store
    store.create(Skill(id="fetch-html", name="a", description="a", params_schema=URL, role="atomic"), FETCH_ATOM, reason="t")
    box = Toolbox(agent.registry)
    caller = SkillCaller(store, NoLLM())

    # 声明了工具权限，但没声明子技能 → 拒绝
    box.restrict(["fetch_page"], None)
    res = await execute_script(TITLE_VIA_ATOM, task="t", params={"url": "http://site/1"}, toolbox=box, llm=NoLLM(), skills=caller)
    assert not res.success and "未声明调用子技能 fetch-html" in res.error
    box.restrict(None, None)

    # 子技能失败 → 调用它的步骤失败
    res = await execute_script(TITLE_VIA_ATOM, task="t", params={"url": "http://nope"}, toolbox=box, llm=NoLLM(), skills=caller)
    assert res.failed_step == "get" and "子技能 fetch-html 在 fetch 失败" in res.error

    # 循环调用
    loop = '@step\nasync def a(ctx):\n    return await ctx.skill("loop", x=1)\n'
    store.create(Skill(id="loop", name="l", description="l"), loop, reason="t")
    res = await execute_script(loop, task="t", params={}, toolbox=box, llm=NoLLM(), skills=SkillCaller(store, NoLLM(), stack=("loop",)))
    assert "循环调用" in res.error


# ── 多候选提炼 + 原子技能 ──


def distill_reply(scripts, atoms=None):
    return lambda m, t: json_reply(
        {
            "use": [1, 2], "id": "page-title", "name": "网页标题", "description": "获取网页标题", "params_schema": URL,
            "scripts": scripts, "atoms": atoms or [],
            "test_params": [{"url": "http://site/2"}, {"url": "http://site/1"}],
        }
    )


async def test_multi_candidate_picks_most_deterministic(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web, distill_candidates=3)
    setup_freeform(strong)
    fast.on("script_llm", lambda m, t: json_reply({"title": re.search(r"<title>(.*?)</title>", m[0]["content"]).group(1)}))
    broken = "@step\nasync def a(ctx):\n    ctx.fail('不行')\n"
    strong.on("distill", distill_reply([broken, TITLE_WITH_LLM, SKILL_V1]))
    web.pages = {f"http://site/{i}": f"<title>Page {i}</title>" for i in (1, 2)}
    async with agent:
        for i in (1, 2):
            await agent.run(f"获取 http://site/{i} 的网页标题")
        await agent.wait_idle()
    h = next(x for x in agent.evolver.history if x["kind"] == "distill" and x["status"] == "created")
    assert h["candidates"] == 3
    # 两个候选都通过测试，选没有 ctx.llm 的那个（更确定、更省）
    assert agent.store.script("page-title") == SKILL_V1


async def test_distill_with_atoms(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web)
    setup_freeform(strong)
    atom = {"id": "fetch-html", "name": "取网页", "description": "获取网页 HTML", "params_schema": URL, "script": FETCH_ATOM}
    strong.on("distill", distill_reply([TITLE_VIA_ATOM], [atom]))
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.9, "params": {"url": _url(m)}}))
    web.pages = {f"http://site/{i}": f"<title>Page {i}</title>" for i in (1, 2, 3)}
    async with agent:
        for i in (1, 2):
            await agent.run(f"获取 http://site/{i} 的网页标题")
        await agent.wait_idle()
        h = next(x for x in agent.evolver.history if x["kind"] == "distill" and x["status"] == "created")
        assert h["atoms"] == ["fetch-html"]
        atom_skill, main = agent.store.get("fetch-html"), agent.store.get("page-title")
        assert atom_skill.role == "atomic" and atom_skill.permissions.tools == ["fetch_page"]
        assert main.permissions.skills == ["fetch-html"] and main.permissions.tools == []
        run = await agent.run("获取 http://site/3 的网页标题")
    assert run.mode == "skill" and run.result == {"title": "Page 3"}


async def test_distill_rejects_unknown_subskill(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web, patch_max_attempts=1)
    setup_freeform(strong)
    strong.on("distill", distill_reply([TITLE_VIA_ATOM]))  # 调用了不存在的 fetch-html，也没有给 atoms
    web.pages = {f"http://site/{i}": f"<title>Page {i}</title>" for i in (1, 2)}
    async with agent:
        for i in (1, 2):
            await agent.run(f"获取 http://site/{i} 的网页标题")
        await agent.wait_idle()
    h = next(x for x in agent.evolver.history if x["kind"] == "distill" and x["status"] == "rejected")
    assert "不存在的技能 fetch-html" in h["detail"]


# ── 按热度升级：指南型 → 流程型 ──

GUIDE = """---
name: page-title
description: 获取网页标题
---

# 获取网页标题

1. 用 fetch_page 打开网址
2. 找到 <title> 标签里的文字
"""


async def test_hot_guide_skill_upgrades_to_script(tmp_path, fast, strong, web):
    agent = make_agent(tmp_path, fast, strong, web, shadow_min_runs=1)
    d = tmp_path / "dl" / "page-title"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(GUIDE, encoding="utf-8")
    agent.install_skill(d)

    def guide(messages, tools):  # AI 照着指南做：抓页面 → 提交结果
        if messages[-1]["role"] == "tool":
            import json

            body = json.loads(messages[-1]["content"])["body"]
            return tool_reply("finish", {"success": True, "result": {"title": re.search(r"<title>(.*?)</title>", body).group(1)}}, "c2")
        return tool_reply("fetch_page", {"url": _url(messages)})

    strong.on("guide", guide)
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.9, "params": {"url": _url(m)}}))
    strong.on("distill", lambda m, t: json_reply({
        "use": [1, 2], "params_schema": URL, "scripts": [SKILL_V1],
        "test_params": [{"url": f"http://site/{i}"} for i in re.findall(r"任务: 获取 http://site/(\d)", m[1]["content"])],
    }))
    web.pages = {f"http://site/{i}": f"<title>Page {i}</title>" for i in range(1, 6)}

    async with agent:
        for i in (1, 2):
            r = await agent.run(f"获取 http://site/{i} 的网页标题")
            assert r.mode == "guide"
        await agent.wait_idle()
        ups = [h for h in agent.evolver.history if h["kind"] == "upgrade"]
        assert "candidate" in [h["status"] for h in ups]  # 第二次成功使用后，提炼出候选脚本
        skill = agent.store.get("page-title")
        assert skill.kind == "guide" and skill.shadow_version == 1  # 转正前仍按指南执行

        r3 = await agent.run("获取 http://site/3 的网页标题")
        assert r3.mode == "guide"
        await agent.wait_idle()
        assert ("shadow", "promoted") in [(h["kind"], h["status"]) for h in agent.evolver.history]
        skill = agent.store.get("page-title")
        assert skill.kind == "flow" and skill.params_schema == URL and skill.permissions.tools == ["fetch_page"]
        assert "1. 用 fetch_page 打开网址" in skill.guide and "scripts/flow_v1.py" in skill.guide  # 原指南保留，追加说明

        strong_calls = len(strong.calls)
        r4 = await agent.run("获取 http://site/4 的网页标题")
        assert r4.mode == "skill" and r4.result == {"title": "Page 4"} and len(strong.calls) == strong_calls


def test_skill_md_body_follows_active_version(agent):
    agent.store.create(Skill(id="page-title", name="标题", description="获取网页标题", params_schema=URL), SKILL_V1, reason="t")
    assert "scripts/flow_v1.py" in agent.store.get("page-title").guide
    agent.store.add_version("page-title", SKILL_V1 + "\n", reason="p", pass_rate=1.0)
    md = (agent.store.path("page-title") / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/flow_v2.py" in md and "flow_v1.py" not in md
