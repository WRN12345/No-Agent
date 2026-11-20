"""脚本沙箱加固与子技能权限委托"""

import asyncio

import pytest

from no_agent.skills.compose import SkillCaller
from no_agent.skills.models import Permissions, Skill
from no_agent.skills.runtime import execute_script
from no_agent.skills.script import ScriptError, load_script, validate_script
from no_agent.storage.files import FileSkillStore
from no_agent.tools.base import Tool, Toolbox, ToolRegistry
from tests.test_script import NoLLM


def _step(body: str, imports: str = "") -> str:
    return f"{imports}\n@step\nasync def a(ctx):\n" + "".join(f"    {line}\n" for line in body.splitlines())


async def _run_step0(src: str):
    return await load_script(src).steps[0].func(None)


# ── 静态校验挡住的逃逸 ──


@pytest.mark.parametrize(
    "src,msg",
    [
        (_step("return 1", "import urllib.request"), "不允许 import urllib.request"),
        (_step("return 1", "from urllib.request import urlopen"), "不允许 from urllib.request import"),
        (_step("return 1", "import dataclasses"), "不允许 import dataclasses"),
        (_step("return 1", "from operator import attrgetter"), "不允许使用 operator.attrgetter"),
        (_step("return 1", "from json import *"), "不允许 import *"),
        (_step("g = (x for x in [1])\nreturn g.gi_frame"), "不允许访问 .gi_frame"),
        (_step("return ctx.f_globals"), "不允许访问 .f_globals"),
        (_step("match ctx:\n    case object(__class__=c):\n        return c"), "不允许在模式匹配中访问 __class__"),
    ],
)
def test_static_escapes_rejected(src, msg):
    with pytest.raises(ScriptError, match=msg):
        validate_script(src)


# ── 运行时：模块代理 ──


@pytest.mark.parametrize(
    "body,imports,msg",
    [
        ("return uuid.os.getcwd()", "import uuid", "不允许经由 uuid 访问模块 os"),
        ("return uuid.sys.modules", "import uuid", "不允许经由 uuid 访问模块 sys"),
        ("return typing.sys", "import typing", "不允许经由 typing 访问模块 sys"),
        ("return operator.attrgetter('x')", "import operator", "不允许使用 operator.attrgetter"),
        ("return string.Formatter()", "import string", "不允许使用 string.Formatter"),
        ("return typing.get_type_hints(a)", "import typing", "不允许使用 typing.get_type_hints"),
        ("return io.open('x')", "import io", "不允许使用 io.open"),
        ("return urllib.request", "import urllib.parse", "不允许经由 urllib 访问模块 urllib.request"),
    ],
)
async def test_module_proxy_blocks_escapes(body, imports, msg):
    import urllib.request  # noqa: F401  确保子模块已加载，验证的是代理而不是"碰巧没加载"

    with pytest.raises(AttributeError, match=msg):
        await _run_step0(_step(body, imports))


async def test_module_proxy_keeps_normal_usage():
    src = _step(
        "d = datetime.datetime(2026, 1, 2).isoformat()\n"
        "q = urllib.parse.urlencode({'a': 1})\n"
        "p = parse.quote('a b')\n"
        "m = isinstance({}, abc.Mapping)\n"
        "c = Counter('aab')['a']\n"
        "return [d, q, p, m, c, json.dumps([1]), re.sub('a', 'b', 'aa'), io.StringIO('x').read()]",
        "import datetime\nimport json\nimport re\nimport io\nimport urllib.parse\n"
        "from urllib import parse\nfrom collections import Counter, abc",
    )
    assert await _run_step0(src) == ["2026-01-02T00:00:00", "a=1", "a%20b", True, 2, "[1]", "bb", "x"]


# ── 子技能权限委托 ──

URL = {"type": "object", "properties": {"url": {"type": "string"}}}

CHILD = '''
@step
async def a(ctx):
    return await ctx.tool("secret", url=ctx.params.get("url", "http://ok.com/"))
'''

PARENT = '''
@step
async def a(ctx):
    return await ctx.skill("child", url="http://ok.com/")
'''


def _registry():
    async def secret(url: str = ""):
        await asyncio.sleep(0)
        return "leaked"

    async def fetch(url: str = ""):
        await asyncio.sleep(0)
        return "page"

    return ToolRegistry([
        Tool("secret", "s", {"type": "object"}, secret, url_arg="url"),
        Tool("fetch", "f", {"type": "object"}, fetch, url_arg="url"),
    ])


def _store(tmp_path, child_perms: Permissions) -> FileSkillStore:
    store = FileSkillStore(tmp_path / "skills")
    store.create(Skill(id="child", name="c", description="c", params_schema=URL, role="atomic", permissions=child_perms), CHILD, reason="t")
    return store


async def test_undeclared_child_inherits_caller_restrictions(tmp_path):
    store = _store(tmp_path, Permissions())  # 子技能什么都没声明
    box = Toolbox(_registry())
    box.restrict(["fetch"], None)  # 调用方只能用 fetch
    res = await execute_script(
        PARENT, task="t", params={}, toolbox=box, llm=NoLLM(), skills=SkillCaller(store, NoLLM()), skill_allowlist=["child"]
    )
    # 修复前：子技能未声明 → restrict([]) → 不受限 → 拿到 secret
    assert not res.success and "技能未声明使用工具 secret" in res.error
    assert box.allowed_tools == {"fetch", "ask_user"}  # 调用方的权限没有被改动


async def test_declared_child_runs_under_own_permissions_but_inherits_domains(tmp_path):
    store = _store(tmp_path, Permissions(tools=["secret"]))  # 声明了工具，没声明域名
    box = Toolbox(_registry())
    box.restrict(["fetch"], ["ok.com"])
    caller = SkillCaller(store, NoLLM())
    res = await execute_script(PARENT, task="t", params={}, toolbox=box, llm=NoLLM(), skills=caller, skill_allowlist=["child"])
    assert res.success and res.result == "leaked"  # 能力委托：子技能用自己声明的工具

    evil = PARENT.replace("http://ok.com/", "http://evil.com/")
    res = await execute_script(evil, task="t", params={}, toolbox=box, llm=NoLLM(), skills=caller, skill_allowlist=["child"])
    assert not res.success and "技能未声明访问域名 evil.com" in res.error  # 域名限制被继承


async def test_concurrent_child_calls_do_not_leak_permissions(tmp_path):
    store = _store(tmp_path, Permissions(tools=["secret"]))
    box = Toolbox(_registry())
    box.restrict(["fetch"], None)
    caller = SkillCaller(store, NoLLM())

    # 修复前：两次调用交错"保存 / 恢复"共享 toolbox 的属性，结束后调用方会残留子技能的 secret 权限

    async def one():
        return await caller.call("child", {}, toolbox=box, task="t")

    assert await asyncio.gather(one(), one()) == ["leaked", "leaked"]
    assert box.effective_permissions() == ({"fetch", "ask_user"}, None)
    res = await execute_script(_step("return await ctx.tool('secret')"), task="t", params={}, toolbox=box, llm=NoLLM())
    assert not res.success and "技能未声明使用工具 secret" in res.error


async def test_untrusted_shared_skill_cannot_be_called(tmp_path):
    shared = tmp_path / "shared"
    FileSkillStore(shared, stats_dir=tmp_path / ".st").create(
        Skill(id="child", name="c", description="c", params_schema=URL, role="atomic"), CHILD, reason="t"
    )
    store = FileSkillStore(tmp_path / "skills", readonly_dirs=[shared], verifier=lambda d: (False, "未签名"))
    assert store.untrusted == {"child"} and store.get("child").shadow is None
    res = await execute_script(PARENT, task="t", params={}, toolbox=Toolbox(_registry()), llm=NoLLM(), skills=SkillCaller(store, NoLLM()))
    assert not res.success and "不可信的共享技能" in res.error
