"""
完整进化闭环（离线，假 LLM）：
  1. 无技能 → AI 自由执行两次同类任务
  2. 后台提炼出技能
  3. 第三次同类任务 → 直接命中脚本，强模型零调用
  4. 网站改版 → 脚本失败 → AI 接管完成 → 后台修补出 v2
  5. 再次运行 → v2 脚本独立完成
"""

import json

from fastapi.testclient import TestClient

from no_agent.server import create_app
from tests.conftest import json_reply, tool_reply

SKILL_V1 = '''
import re

@step
async def fetch(ctx):
    r = await ctx.tool("fetch_page", url=ctx.params["url"])
    ctx.state["html"] = r["body"]


@step
async def extract(ctx):
    m = re.search(r"<title>(.*?)</title>", ctx.state["html"])
    if not m:
        ctx.fail("页面中没有 <title>")
    return {"title": m.group(1)}


def verify(ctx, result):
    return bool(result.get("title")) or "title 为空"
'''

EXTRACT_V2 = '''@step
async def extract(ctx):
    m = re.search(r"<title>(.*?)</title>", ctx.state["html"], re.I | re.S)
    if not m:
        ctx.fail("页面中没有 <title>")
    return {"title": m.group(1).strip()}'''


def _url(messages):
    import re

    return re.search(r"http://\S+", messages[1]["content"]).group(0)


def setup_freeform(strong):
    """强模型自由执行：先抓页面，再 finish"""

    def freeform(messages, tools):
        if messages[-1]["role"] == "tool":
            import re

            body = json.loads(messages[-1]["content"])["body"]
            title = re.search(r"<title>(.*?)</title>", body, re.I).group(1)
            return tool_reply("finish", {"success": True, "result": {"title": title}}, "c2")
        return tool_reply("fetch_page", {"url": _url(messages)})

    strong.on("freeform", freeform)


async def test_full_evolution_loop(agent, fast, strong, web):
    ev = agent.evolver
    ev.start()
    web.pages = {f"http://site/{i}": f"<html><title>Page {i}</title></html>" for i in range(1, 6)}
    setup_freeform(strong)

    # ── 1. 自由执行两次 ──
    r1 = await agent.run("获取 http://site/1 的网页标题")
    await ev.drain()
    assert r1.mode == "freeform" and r1.success and r1.result == {"title": "Page 1"}
    assert ev.history[-1]["status"] == "waiting"  # 只有 1 条轨迹，继续等

    # ── 2. 第二次后提炼技能 ──
    strong.on(
        "distill",
        lambda m, t: json_reply(
            {
                "skip": False,
                "use": [1, 2],
                "id": "page-title",
                "name": "获取网页标题",
                "description": "抓取指定网页并提取 <title> 标题",
                "when_to_use": "用户要获取某个网址的网页标题",
                "keywords": ["网页", "标题", "title"],
                "params_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                "script": SKILL_V1,
                "test_params": [{"url": "http://site/2"}, {"url": "http://site/1"}],
            }
        ),
    )
    r2 = await agent.run("获取 http://site/2 的网页标题")
    await ev.drain()
    assert r2.mode == "freeform"
    assert ev.history[-1]["status"] == "created", ev.history[-1]
    skill = agent.store.get("page-title")
    assert skill and skill.active_version == 1 and len(skill.tests) == 2
    assert agent.runs.get(r1.id).distilled_into == "page-title"

    # ── 3. 直接命中技能，强模型零调用 ──
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.95, "params": {"url": _url(m)}}))
    strong_calls_before = len(strong.calls)
    r3 = await agent.run("获取 http://site/3 的网页标题")
    assert r3.mode == "skill" and r3.success and r3.result == {"title": "Page 3"}
    assert len(strong.calls) == strong_calls_before
    assert r3.prompt_tokens + r3.completion_tokens == 120  # 只有一次小模型匹配调用

    # ── 4. 网站改版：大写 TITLE → 脚本失败 → AI 接管 → 修补 v2 ──
    web.pages["http://site/4"] = "<html><TITLE>\n  Page 4 \n</TITLE></html>"

    def takeover(messages, tools):
        system = messages[0]["content"]
        assert "失败的步骤：extract" in system and "页面中没有 <title>" in system
        return tool_reply("finish", {"success": True, "result": {"title": "Page 4"}})

    strong.on("takeover", takeover)
    strong.on("patch", lambda m, t: json_reply({"mode": "functions", "functions": {"extract": EXTRACT_V2}, "reason": "标签大小写不敏感"}))

    r4 = await agent.run("获取 http://site/4 的网页标题")
    assert r4.mode == "skill_takeover" and r4.success and r4.result == {"title": "Page 4"}
    await ev.drain()
    assert ev.history[-1]["status"] == "promoted", ev.history[-1]
    skill = agent.store.get("page-title")
    assert skill.active_version == 2 and len(skill.tests) == 3
    assert skill.stats.takeovers == 1
    assert "def fetch" in agent.store.script("page-title", 2)  # 未出错的步骤原样保留

    # ── 5. v2 独立完成同类改版页面 ──
    web.pages["http://site/5"] = "<TITLE>Page 5</TITLE>"
    r5 = await agent.run("获取 http://site/5 的网页标题")
    assert r5.mode == "skill" and r5.skill_version == 2 and r5.result == {"title": "Page 5"}
    await ev.stop()


async def test_patch_rejected_when_tests_fail(agent, fast, strong, web):
    """修补把老用例搞坏 → 不上线"""
    ev = agent.evolver
    ev.start()
    web.pages = {"http://site/1": "<title>A</title>", "http://site/2": "<title>B</title>", "http://site/3": "<TITLE>C</TITLE>"}
    setup_freeform(strong)
    strong.on(
        "distill",
        lambda m, t: json_reply(
            {
                "use": [1, 2],
                "id": "page-title",
                "name": "t",
                "description": "获取网页标题",
                "params_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                "script": SKILL_V1,
                "test_params": [{"url": "http://site/2"}, {"url": "http://site/1"}],
            }
        ),
    )
    await agent.run("获取 http://site/1 的网页标题")
    await agent.run("获取 http://site/2 的网页标题")
    await ev.drain()

    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.9, "params": {"url": _url(m)}}))
    strong.on("takeover", lambda m, t: tool_reply("finish", {"success": True, "result": {"title": "C"}}))
    bad = "@step\nasync def extract(ctx):\n    return {'title': ''}"
    strong.on("patch", lambda m, t: json_reply({"mode": "functions", "functions": {"extract": bad}}))

    await agent.run("获取 http://site/3 的网页标题")
    await ev.drain()
    assert ev.history[-1]["status"] == "rejected"
    assert strong.calls.count("patch") == 2  # 带着测试失败信息重试过一次
    assert agent.store.get("page-title").active_version == 1
    await ev.stop()


async def test_no_candidates_skips_match_llm(agent, fast, strong, web):
    """技能库为空时不调用匹配模型；纯问答不触发提炼"""
    from no_agent.llm import LLMReply

    strong.on("freeform", lambda m, t: LLMReply(content="你好！"))
    run = await agent.run("你好")
    assert run.success and run.result == "你好！"
    assert fast.calls == []
    assert agent.evolver.queue.qsize() == 0


def test_api(agent, strong, web):
    web.pages = {"http://site/1": "<title>X</title>"}
    setup_freeform(strong)
    with TestClient(create_app(agent), client=("127.0.0.1", 50000)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.post("/api/v1/run", json={"task": ""}).status_code == 422

        r = client.post("/api/v1/run", json={"task": "获取 http://site/1 的网页标题"}).json()
        assert r["success"] and r["mode"] == "freeform" and r["tool_calls"] == 1

        detail = client.get(f"/api/v1/runs/{r['run_id']}").json()
        assert detail["trace"]["tool_calls"][0]["tool"] == "fetch_page"

        fb = client.post("/api/v1/feedback", json={"run_id": r["run_id"], "score": -1, "comment": "不对"}).json()
        assert fb["ok"] and not fb["patch_queued"]
        assert client.get("/api/v1/skills").json() == []
        assert [t["name"] for t in client.get("/api/v1/tools").json()] == ["ask_user", "fetch_page"]
