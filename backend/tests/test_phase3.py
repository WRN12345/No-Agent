"""
阶段 3：MCP 服务端 / 客户端、browser-use 集成

- MCP 服务端：用内存传输连接真实的 MCP ClientSession
- MCP 客户端：以 stdio 启动真实的 MCP 服务进程（tests/fixtures/mcp_shop.py）
- browser-use：替身模块遵循已核对的 browser-use 1.0 接口，并通过真实 CDP 端口接入框架启动的 Chrome
"""

import json
import sys
import types as pytypes
from pathlib import Path

import pytest

from no_agent import Agent, AgentConfig, BrowserConfig, ScriptedAsker, Skill, UserAbort
from no_agent.config import McpServerConfig
from no_agent.llm import LLMs
from no_agent.tools.base import Toolbox
from tests.conftest import json_reply, tool_reply
from tests.test_evolution import SKILL_V1

mcp = pytest.importorskip("mcp")
import mcp.types as mt  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from no_agent.integrations.mcp_server import create_mcp_server  # noqa: E402

URL_SCHEMA = {"type": "object", "properties": {"url": {"type": "string", "description": "网址"}}, "required": ["url"]}

ASK_SCRIPT = '''
@step
async def ask(ctx):
    a = await ctx.ask([{"key": "#email", "label": "邮箱"}, {"key": "dept", "label": "部门", "kind": "choice", "options": ["财务", "技术"]}])
    return {"email": a["#email"], "dept": a["dept"]}
'''


def text(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def served(agent):
    agent.store.create(
        Skill(id="page-title", name="网页标题", description="获取网页标题", params_schema=URL_SCHEMA, permissions={"tools": ["fetch_page"]}),
        SKILL_V1,
        reason="t",
    )
    agent.store.create(Skill(id="ask-info", name="问信息", description="向用户收集信息"), ASK_SCRIPT, reason="t")
    return create_mcp_server(agent)


# ── MCP 服务端 ──


async def test_mcp_server_lists_and_calls_skills(agent, served, web, strong):
    web.pages = {"http://site/1": "<title>MCP</title>"}
    async with create_connected_server_and_client_session(served) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"run_task", "list_skills", "skill_page-title", "skill_ask-info"} <= set(tools)
        pt = tools["skill_page-title"]
        assert pt.inputSchema["required"] == ["url"] and pt.annotations.readOnlyHint is True  # 只用了只读工具

        r = text(await client.call_tool("skill_page-title", {"url": "http://site/1"}))
        assert r["mode"] == "skill" and r["result"] == {"title": "MCP"} and r["tokens"] == 0

        bad = await client.call_tool("skill_page-title", {})
        assert bad.isError and "缺少必填参数 url" in bad.content[0].text

        skills = text(await client.call_tool("list_skills", {}))
        assert {s["id"] for s in skills} == {"page-title", "ask-info"}

        res = (await client.list_resources()).resources
        assert {str(x.uri) for x in res} == {"skill://page-title/SKILL.md", "skill://ask-info/SKILL.md"}
        md = (await client.read_resource("skill://page-title/SKILL.md")).contents[0].text
        assert md.startswith("---\nname: page-title")

        strong.on("freeform", lambda m, t: tool_reply("finish", {"success": True, "result": "done"}))
        r = text(await client.call_tool("run_task", {"task": "随便做点什么", "mode": "freeform"}))
        assert r["mode"] == "freeform" and r["result"] == "done"
    await agent.stop()


async def test_mcp_ask_becomes_elicitation(agent, served):
    seen = {}

    async def elicit(context, params):
        seen["message"], seen["schema"] = params.message, params.requestedSchema
        return mt.ElicitResult(action="accept", content={"q0": "a@b.c", "q1": "技术"})

    async with create_connected_server_and_client_session(served, elicitation_callback=elicit) as client:
        r = text(await client.call_tool("skill_ask-info", {}))
    assert r["result"] == {"email": "a@b.c", "dept": "技术"}
    props = seen["schema"]["properties"]
    assert props["q0"]["title"] == "邮箱" and props["q1"]["enum"] == ["财务", "技术"]  # 特殊字符的 key 被映射掉
    assert seen["schema"]["required"] == ["q0", "q1"]
    await agent.stop()


async def test_mcp_decline_or_no_elicitation_aborts(agent, served):
    async def decline(context, params):
        return mt.ElicitResult(action="decline")

    async with create_connected_server_and_client_session(served, elicitation_callback=decline) as client:
        r = text(await client.call_tool("skill_ask-info", {}))
    assert r["mode"] == "aborted" and "拒绝" in r["error"]

    async with create_connected_server_and_client_session(served) as client:  # 客户端不支持 elicitation
        r = text(await client.call_tool("skill_ask-info", {}))
    assert r["mode"] == "aborted" and "不支持 elicitation" in r["error"]
    await agent.stop()


# ── MCP 客户端 ──

SHOP = McpServerConfig(name="shop", command=sys.executable, args=[str(Path(__file__).parent / "fixtures" / "mcp_shop.py")])


async def test_mcp_client_registers_external_tools(tmp_path, fast, strong):
    asker = ScriptedAsker(confirm=False)
    agent = Agent(
        AgentConfig(home=tmp_path / ".no-agent", mcp_servers=[SHOP], shadow_min_runs=0),
        include_builtin_tools=False,
        llms=LLMs(fast=fast, strong=strong),
        asker=asker,
    )
    async with agent:
        lookup, cancel, note = (agent.registry.get(f"shop__{n}") for n in ("lookup", "cancel", "add_note"))
        assert lookup and cancel and note
        # 注解映射：只读 → 无副作用；destructive → 需确认；未标注 → 有副作用但不强制确认
        assert (lookup.side_effect, lookup.confirm) == (False, False)
        assert (cancel.side_effect, cancel.confirm) == (True, True)
        assert (note.side_effect, note.confirm) == (True, False)

        box = Toolbox(agent.registry, asker=asker)
        assert await box.call("shop__lookup", {"order_id": "A1"}) == {"id": "A1", "status": "shipped"}
        with pytest.raises(Exception, match="订单不存在"):
            await box.call("shop__lookup", {"order_id": "ZZ"})
        with pytest.raises(UserAbort):
            await box.call("shop__cancel", {"order_id": "A1"})  # 用户拒绝 → 没有执行
        assert await box.call("shop__lookup", {"order_id": "A1"}) == {"id": "A1", "status": "shipped"}

        # AI 自由执行时可以使用外部工具，调用会录进轨迹（之后可以被提炼进技能）
        def freeform(messages, tools):
            if messages[-1]["role"] == "tool":
                return tool_reply("finish", {"success": True, "result": json.loads(messages[-1]["content"])}, "c2")
            assert any(t["function"]["name"] == "shop__lookup" for t in tools)
            return tool_reply("shop__lookup", {"order_id": "B2"})

        strong.on("freeform", freeform)
        run = await agent.run("查一下订单 B2 的状态")
        assert run.result == {"id": "B2", "status": "pending"}
        assert run.trace["tool_calls"][0]["tool"] == "shop__lookup"
    assert agent.registry.get("shop__lookup") is not None  # 工具定义保留，连接已关闭


# ── browser-use ──


@pytest.fixture
def fake_browser_use(monkeypatch):
    """遵循 browser-use 1.0 接口的替身：通过真实 CDP 端口接入浏览器、新开标签页并导航"""
    calls = {}

    class ChatOpenAI:
        def __init__(self, model, *, api_key=None, base_url=None, **kw):
            calls["llm"] = {"model": model, "base_url": base_url}

    class Browser:
        def __init__(self, *, cdp_url=None, keep_alive=False, **kw):
            calls["browser"] = {"cdp_url": cdp_url, "keep_alive": keep_alive}
            self.cdp_url = cdp_url

    class History:
        def __init__(self, url):
            self.url = url

        def is_done(self):
            return True

        def is_successful(self):
            return True

        def final_result(self):
            return f"已打开 {self.url}"

        def number_of_steps(self):
            return 3

        def errors(self):
            return [None, None]

    class FakeAgent:
        def __init__(self, task, llm=None, *, browser=None, judge=True, **kw):
            calls["task"], calls["judge"], self.browser = task, judge, browser

        async def run(self, max_steps=100, **kw):
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                b = await p.chromium.connect_over_cdp(self.browser.cdp_url)
                page = await b.contexts[0].new_page()
                await page.goto(calls["target"])
                await b.close()  # 只断开 CDP 连接，浏览器本身继续运行
            return History(calls["target"])

    mod = pytypes.ModuleType("browser_use")
    mod.Agent, mod.Browser, mod.ChatOpenAI = FakeAgent, Browser, ChatOpenAI
    monkeypatch.setitem(sys.modules, "browser_use", mod)
    return calls


async def test_browser_agent_shares_browser_via_cdp(tmp_path, fast, strong, fake_browser_use):
    from tests.test_browser import FORM

    form = tmp_path / "form.html"
    form.write_text(FORM, encoding="utf-8")
    fake_browser_use["target"] = form.as_uri()

    asker = ScriptedAsker(confirm=True)
    cfg = AgentConfig(home=tmp_path / ".no-agent", browser=BrowserConfig(enabled=True, headless=True, browser_use=True))
    agent = Agent(cfg, include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=asker)
    try:
        await agent.browser.page()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"没有可用的浏览器: {e}")
    try:
        box = Toolbox(agent.registry, asker=asker)
        out = await box.call("browser_agent", {"task": "找到报销申请页面"})
        assert out["done"] and out["steps"] == 3 and out["title"] == "报销申请"
        assert asker.confirmations and "browser-use" in asker.confirmations[0]  # 交给它之前请求了确认
        assert fake_browser_use["browser"]["keep_alive"] is True and fake_browser_use["judge"] is False
        assert "不要点击提交" in fake_browser_use["task"]

        # browser-use 停下的页面上，确定性工具直接接着用
        snap = await box.call("browser_snapshot", {})
        assert [f["label"] for f in snap["fields"]][:2] == ["姓名", "邮箱"]
    finally:
        await agent.stop()


async def test_browser_agent_requires_confirmation_by_default(tmp_path, fast, strong, fake_browser_use):
    cfg = AgentConfig(home=tmp_path / ".no-agent", browser=BrowserConfig(enabled=True, headless=True, browser_use=True))
    agent = Agent(cfg, include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=ScriptedAsker(confirm=False))
    with pytest.raises(UserAbort):
        await Toolbox(agent.registry, asker=agent.asker).call("browser_agent", {"task": "x"})
    assert "llm" not in fake_browser_use  # 拒绝后根本没有启动 browser-use
    await agent.stop()


async def test_cli_mcp_server_end_to_end_over_stdio(tmp_path):
    """本框架的 MCP 客户端 ↔ `no-agent mcp` 子进程（stdio）：列出并调用技能，无需任何 LLM"""
    import os

    from no_agent import FileSkillStore
    from no_agent.integrations.mcp_client import McpToolset

    home = tmp_path / ".no-agent"
    calc = '@step\nasync def calc(ctx):\n    return {"value": await ctx.tool("calculator", expression=ctx.params["expr"])}\n'
    FileSkillStore(home / "skills").create(
        Skill(id="calc", name="计算", description="计算表达式",
              params_schema={"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}),
        calc,
        reason="t",
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "NO_AGENT_HOME": str(home), "PYTHONPATH": src, "NO_AGENT_LLM__API_KEY": "unused"}
    toolset = McpToolset(McpServerConfig(name="na", command=sys.executable, args=["-m", "no_agent.cli", "mcp"], env=env))
    tools = {t.name: t for t in await toolset.connect()}
    try:
        assert {"na__run_task", "na__list_skills", "na__skill_calc"} <= set(tools)
        out = await tools["na__skill_calc"].invoke({"expr": "(2 + 3) * 4"})
        assert out["mode"] == "skill" and out["result"] == {"value": 20} and out["tokens"] == 0
    finally:
        await toolset.close()
