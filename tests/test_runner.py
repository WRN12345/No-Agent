"""Runner 抽象：编排在宿主、脚本执行位置可替换（P1：进程内）"""

import asyncio
import subprocess
import sys
from pathlib import Path

from no_agent.skills import sandbox
from no_agent.skills.runner import InProcessRunner
from no_agent.skills.runtime import execute_script
from no_agent.skills.sandbox import step_meta
from no_agent.tools.base import Tool, Toolbox, ToolRegistry
from tests.test_script import NoLLM


def _box() -> Toolbox:
    async def slow():
        await asyncio.sleep(5)
        return "late"

    async def read():
        return "data"

    return Toolbox(ToolRegistry([Tool("slow", "s", {"type": "object"}, slow), Tool("read", "r", {"type": "object"}, read)]))


async def test_verify_has_timeout():
    src = '@step\nasync def a(ctx):\n    return 1\n\nasync def verify(ctx, result):\n    return await ctx.tool("slow")\n'
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM(), step_timeout=0.3)
    assert not res.success and res.failed_step == "verify" and res.error == "verify 超时（0.3s）"
    assert res.result == 1  # 快照仍然交回


async def test_guard_rejects_unregistered_tool():
    src = '@step\nasync def a(ctx):\n    return 1\n\nasync def guard(ctx):\n    await ctx.tool("nope")\n    return True\n'
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM())
    assert res.guard_rejected and "guard 中只能调用只读工具，不能调用 nope" in res.error


async def test_guard_allows_read_only_tool():
    src = '@step\nasync def a(ctx):\n    return ctx.state["d"]\n\nasync def guard(ctx):\n    ctx.state["d"] = await ctx.tool("read")\n    return True\n'
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM())
    assert res.success and res.result == "data"


def test_step_meta_is_static_and_conservative():
    src = (
        "@step\nasync def a(ctx):\n    pass\n\n"
        "@step(idempotent=False, kind='llm')\nasync def b(ctx):\n    pass\n\n"
        "FLAG = True\n\n@step(idempotent=FLAG)\nasync def c(ctx):\n    pass\n"
    )
    assert [(m.name, m.idempotent, m.kind) for m in step_meta(src)] == [
        ("a", True, "code"), ("b", False, "llm"), ("c", False, "code"),  # 非字面量 → 按有副作用处理
    ]


async def test_dynamically_registered_step_is_rejected():
    # 顶层赋值里调用 step(...) 会在加载时多注册一个步骤，静态解析看不到 → 拒绝加载
    src = "@step\nasync def a(ctx):\n    return 1\n\nb = step(a)\n"
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM())
    assert res.failed_step == "<load>" and "步骤列表与静态解析不一致" in res.error


async def test_custom_runner_is_used():
    opened = []

    class Recording(InProcessRunner):
        async def open(self, source, **kw):
            opened.append(source)
            return await super().open(source, **kw)

    src = "@step\nasync def a(ctx):\n    return 42\n"
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM(), runner=Recording())
    assert res.success and res.result == 42 and opened == [src]


def test_sandbox_module_loads_standalone():
    """P2 的 worker 会按文件路径加载 sandbox.py：它不能依赖 no_agent 包"""
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('sb', {str(Path(sandbox.__file__))!r})\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['sb'] = m; spec.loader.exec_module(m)\n"
        "assert not [k for k in sys.modules if k.startswith('no_agent')]\n"
        "print(m.load_script('@step\\nasync def a(ctx):\\n    return 1\\n').steps[0].name)\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "a"
