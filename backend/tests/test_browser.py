"""
浏览器 + 人机交互（真实 Chrome，headless；没有可用浏览器时跳过）

  - snapshot 一次读出所有字段与按钮，选择器稳定
  - 内置 fill_form 技能：已知信息自动填、缺失项一次问完、回读校验、提交前确认
  - 用户拒绝确认 → aborted，不接管、不修补、不计统计
  - 带问答的运行可以离线回放测试（不弹问题、不真的提交）
"""

import pytest

from no_agent import Agent, AgentConfig, BrowserConfig, ScriptedAsker
from no_agent.engine.testing import run_tests
from no_agent.llm import LLMs
from no_agent.skills.models import TestCase
from tests.conftest import json_reply

FORM = """<!doctype html><html><head><meta charset="utf-8"><title>报销申请</title></head><body>
<form id="f">
  <label for="name">姓名</label><input id="name" required>
  <label for="email">邮箱</label><input id="email" type="email" required>
  <label>部门 <select name="dept" required><option value="">请选择</option><option value="fin">财务部</option><option value="tech">技术部</option></select></label>
  <fieldset><legend>性别</legend>
    <label><input type="radio" name="gender" value="m">男</label>
    <label><input type="radio" name="gender" value="f">女</label>
  </fieldset>
  <textarea name="note" placeholder="备注"></textarea>
  <label><input type="checkbox" id="agree" required>我已阅读报销制度</label>
  <input type="hidden" name="token" value="x">
  <button type="button" id="draft">存草稿</button>
  <button id="submit-btn">提交</button>
</form>
<div id="result"></div>
<script>
document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  const d = new FormData(e.target);
  document.getElementById('result').innerText =
    '提交成功：' + [d.get('dept'), document.getElementById('name').value, document.getElementById('email').value].join(',');
});
</script></body></html>"""


@pytest.fixture
def form_url(tmp_path):
    p = tmp_path / "form.html"
    p.write_text(FORM, encoding="utf-8")
    return p.as_uri()


def make_agent(tmp_path, fast, strong, asker):
    cfg = AgentConfig(home=tmp_path / ".no-agent", browser=BrowserConfig(enabled=True, headless=True))
    return Agent(cfg, include_builtin_tools=False, llms=LLMs(fast=fast, strong=strong), asker=asker)


@pytest.fixture
async def browser_ok(tmp_path, fast, strong):
    """探测本机是否能启动浏览器"""
    agent = make_agent(tmp_path / "probe", fast, strong, None)
    try:
        await agent.browser.page()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"没有可用的浏览器: {e}")
    finally:
        await agent.browser.close()


def setup_fill_form(fast, url):
    fast.on(
        "match",
        lambda m, t: json_reply({"skill_id": "fill_form", "confidence": 0.9, "params": {"url": url, "data": "姓名张三，技术部"}}),
    )
    fast.on("script_llm", lambda m, t: json_reply({"values": {"#name": "张三", 'select[name="dept"]': "技术部"}, "submit": "#submit-btn"}))


async def test_snapshot_reads_all_fields_once(tmp_path, fast, strong, form_url, browser_ok):
    async with make_agent(tmp_path, fast, strong, None) as agent:
        from no_agent.tools.base import Toolbox

        box = Toolbox(agent.registry)
        await box.call("browser_open", {"url": form_url})
        snap = await box.call("browser_snapshot", {})

        fields = {f["label"]: f for f in snap["fields"]}
        assert list(fields) == ["姓名", "邮箱", "部门", "性别", "备注", "我已阅读报销制度"]
        assert fields["姓名"]["selector"] == "#name" and fields["姓名"]["required"]
        dept = fields["部门"]
        assert dept["selector"] == 'select[name="dept"]' and [o["label"] for o in dept["options"]] == ["请选择", "财务部", "技术部"]
        assert fields["性别"]["type"] == "radio" and [o["label"] for o in fields["性别"]["options"]] == ["男", "女"]
        assert fields["备注"]["selector"] == 'textarea[name="note"]'
        assert {b["text"]: b["submit"] for b in snap["buttons"]} == {"存草稿": False, "提交": True}

        await box.call(
            "browser_fill",
            {"fields": [{"selector": "#name", "value": "李四"}, {"selector": dept["selector"], "value": "财务部"},
                        {"selector": fields["性别"]["selector"], "value": "女"}, {"selector": "#agree", "value": True}]},
        )
        got = await box.call("browser_read", {"selectors": ["#name", dept["selector"], fields["性别"]["selector"], "#agree"]})
        assert got == {"#name": "李四", 'select[name="dept"]': {"value": "fin", "label": "财务部"}, 'input[type="radio"][name="gender"]': "f", "#agree": True}

        # 提交按钮不能用 click 绕过确认
        with pytest.raises(Exception, match="browser_submit"):
            await box.call("browser_click", {"selector": "#submit-btn"})


async def test_fill_form_asks_once_confirms_and_submits(tmp_path, fast, strong, form_url, browser_ok):
    setup_fill_form(fast, form_url)
    asker = ScriptedAsker({"#email": "zs@example.com", "#agree": True}, confirm=True)
    async with make_agent(tmp_path, fast, strong, asker) as agent:
        run = await agent.run(f"帮我填一下报销申请 {form_url}，我叫张三，技术部")
        assert run.mode == "skill" and run.success, run.error
        assert run.result["submitted"] is True
        assert run.result["filled"]["邮箱"] == "zs@example.com"
        assert "提交成功：tech,张三,zs@example.com" in run.result["after"]["text"]

        # 缺失的必填项一次问完；提交前框架强制确认，确认内容里带着填写摘要
        assert len(asker.asked) == 1 and {q.key for q in asker.asked[0]} == {"#email", "#agree"}
        assert len(asker.confirmations) == 1 and "张三" in asker.confirmations[0]
        assert strong.calls == []  # 全程没用到强模型
        assert set(fast.calls) == {"match", "script_llm"}

        # 带问答的运行可以离线回放：不弹问题、不真的提交
        case = TestCase(task=run.task, params=run.trace["match"]["params"], fixtures=run.trace["fixtures"])
        report = await run_tests(agent.store.script("fill_form"), [case], registry=agent.registry, llm=fast)
        assert report.passed == 1, report.failures
        assert len(asker.asked) == 1 and len(asker.confirmations) == 1


async def test_declined_confirmation_aborts_without_evolving(tmp_path, fast, strong, form_url, browser_ok):
    setup_fill_form(fast, form_url)
    asker = ScriptedAsker({"#email": "zs@example.com", "#agree": True}, confirm=False)
    async with make_agent(tmp_path, fast, strong, asker) as agent:
        run = await agent.run(f"帮我填一下报销申请 {form_url}")
        assert run.mode == "aborted" and not run.success and "用户取消" in run.error
        assert strong.calls == []  # 没有触发 AI 接管
        assert agent.evolver.queue.qsize() == 0  # 没有触发修补
        assert agent.store.get("fill_form").stats.runs == 0  # 不计入技能统计
        page = await agent.browser.page()
        assert await page.inner_text("#result") == ""  # 确实没有提交


async def test_no_asker_refuses_confirmation(tmp_path, fast, strong, form_url, browser_ok):
    """非交互环境（如服务端）没有 asker：需要确认的提交一律拒绝，除非显式开启 auto_confirm"""
    setup_fill_form(fast, form_url)
    from no_agent.interact import ScriptedAsker as SA

    asker = SA({"#email": "a@b.c", "#agree": True}, confirm=True)
    agent = make_agent(tmp_path, fast, strong, asker)
    agent.engine.asker = None  # 模拟没有询问渠道
    async with agent:
        run = await agent.run(f"填表 {form_url}")
        assert run.mode == "aborted" and "没有可用的询问方式" in run.error
