"""P3：sandbox 档（Linux 上的 Landlock + seccomp）

过滤器逻辑、ABI 结构体、失败即拒绝启动在任何平台上都能测；真实的内核约束只在 Linux 上测。
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from no_agent.skills import _confine as c
from no_agent.skills.process import ProcessRunner, WorkerPool, WorkerStartError
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import Tool, Toolbox, ToolRegistry
from tests.test_script import NoLLM

LINUX = sys.platform.startswith("linux")
PID = 4242


# ── seccomp 过滤器：用一个小型 BPF 解释器逐条验证判定结果 ──


def run_bpf(prog, *, arch: int, nr: int, arg0: int = 0) -> int:
    data = {0: nr, 4: arch, 16: arg0 & 0xFFFFFFFF}
    acc, pc = 0, 0
    for _ in range(10_000):
        code, jt, jf, k = prog[pc]
        if code == 0x20:
            acc = data[k]
            pc += 1
        elif code == 0x06:
            return k
        elif code in (0x15, 0x35, 0x45):
            hit = acc == k if code == 0x15 else acc >= k if code == 0x35 else bool(acc & k)
            pc += 1 + (jt if hit else jf)
        else:
            raise AssertionError(f"未知指令 {code:#x}")
        assert 0 <= pc < len(prog), "跳转越界"
    raise AssertionError("过滤器没有返回")


EPERM, ENOSYS = c.ret_errno(c.EPERM), c.ret_errno(c.ENOSYS)


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_seccomp_filter_decisions(arch):
    prog = c.build_filter(arch, PID)
    a = c.AUDIT_ARCH[arch]
    deny, kill = c._DENY[arch], c._KILL[arch]
    for name in ("socket", "socketpair", "connect", "execve", "execveat", "ptrace", "unshare", "io_uring_setup", "bpf"):
        assert run_bpf(prog, arch=a, nr=deny[name]) == EPERM, name
    if arch == "x86_64":
        assert run_bpf(prog, arch=a, nr=deny["fork"]) == EPERM
        assert run_bpf(prog, arch=a, nr=deny["vfork"]) == EPERM
    clone = c._CLONE[arch]
    assert run_bpf(prog, arch=a, nr=clone, arg0=c.CLONE_THREAD | 0x3D0F00) == c.RET_ALLOW  # 线程
    assert run_bpf(prog, arch=a, nr=clone, arg0=0x01200011) == EPERM  # fork 的 clone 标志
    assert run_bpf(prog, arch=a, nr=c._CLONE3) == ENOSYS
    for name, nr in kill.items():
        assert run_bpf(prog, arch=a, nr=nr, arg0=PID) == c.RET_ALLOW, name
        assert run_bpf(prog, arch=a, nr=nr, arg0=1) == EPERM, name
        assert run_bpf(prog, arch=a, nr=nr, arg0=0) == EPERM, name  # 进程组（包括宿主）
        assert run_bpf(prog, arch=a, nr=nr, arg0=-PID) == EPERM, name
    for nr in (0, 1, 3, 9, 60, 231):  # read / write / close / mmap / exit / exit_group 之类
        if nr not in deny.values() and nr not in kill.values() and nr != clone:
            assert run_bpf(prog, arch=a, nr=nr) == c.RET_ALLOW
    other = c.AUDIT_ARCH["aarch64" if arch == "x86_64" else "x86_64"]
    assert run_bpf(prog, arch=other, nr=0) == c.RET_KILL_PROCESS
    assert run_bpf(prog, arch=0x40000003, nr=0) == c.RET_KILL_PROCESS  # i386 兼容调用
    if arch == "x86_64":
        assert run_bpf(prog, arch=a, nr=0x40000000 | 41) == c.RET_KILL_PROCESS  # x32 ABI


def test_unsupported_arch_fails_before_anything():
    with pytest.raises(c.ConfinementError, match="不支持的 CPU 架构"):
        c.build_filter("riscv64", PID)


def test_landlock_ruleset_layout():
    assert len(c.ruleset_attr(1)) == 8 and len(c.ruleset_attr(4)) == 16 and len(c.ruleset_attr(6)) == 24
    assert c.handled_fs(1) == (1 << 13) - 1 and c.handled_fs(3) == (1 << 15) - 1 and c.handled_fs(5) == (1 << 16) - 1
    assert c._PathBeneath.__dict__["allowed_access"].offset == 0 and c.ctypes.sizeof(c._PathBeneath) == 12


@pytest.mark.skipif(not LINUX, reason="只在 Linux 上有系统调用头文件")
@pytest.mark.parametrize("header", ["/usr/include/x86_64-linux-gnu/asm/unistd_64.h", "/usr/include/asm-generic/unistd.h"])
def test_syscall_numbers_match_kernel_headers(header):
    arch = "x86_64" if "x86_64" in header else "aarch64"
    p = Path(header)
    if not p.exists():
        pytest.skip(f"没有 {header}")
    table = {m[0]: int(m[1]) for m in re.findall(r"#define __NR(?:3264)?_(\w+)\s+(\d+)", p.read_text())}
    expected = {**c._DENY[arch], **c._KILL[arch], "clone": c._CLONE[arch], "clone3": c._CLONE3}
    if arch == "aarch64":
        expected = {k: v for k, v in expected.items() if k not in ("fork", "vfork")}
    mismatched = {k: (v, table.get(k)) for k, v in expected.items() if k in table and table[k] != v}
    assert not mismatched


# ── 失败即拒绝启动 ──


def _box() -> Toolbox:
    async def echo(text: str = ""):
        return text

    return Toolbox(ToolRegistry([Tool("echo", "e", {"type": "object"}, echo)]))


SRC = '@step\nasync def a(ctx):\n    return await ctx.tool("echo", text="ok")\n'


@pytest.mark.skipif(LINUX, reason="Linux 上 worker 能进入 sandbox")
async def test_sandbox_worker_refuses_to_start_where_unsupported():
    with pytest.raises(WorkerStartError, match="无法进入 sandbox"):
        await execute_script(SRC, task="t", params={}, toolbox=_box(), llm=NoLLM(), runner=ProcessRunner(WorkerPool(0, 256, 60, sandbox=True)))


@pytest.mark.skipif(LINUX, reason="Linux 上支持 sandbox 档")
def test_sandbox_config_rejected_off_linux():
    from no_agent import AgentConfig
    from no_agent.skills.runner import runner_for

    with pytest.raises(ValueError, match="只支持 Linux"):
        runner_for(AgentConfig(script_isolation="sandbox"))


async def test_untrusted_shadow_skills_never_run_outside_sandbox(tmp_path):
    from no_agent import Agent, AgentConfig, FileSkillStore, Skill
    from no_agent.llm import LLMs

    shared = tmp_path / "shared"
    FileSkillStore(shared, stats_dir=tmp_path / ".st").create(Skill(id="evil", name="e", description="e"), SRC, reason="t")
    agent = Agent(
        AgentConfig(home=tmp_path / ".na", extra_skill_dirs=[shared], untrusted_shared="shadow", script_isolation="process"),
        include_builtin_tools=False, llms=LLMs(fast=NoLLM(), strong=NoLLM()), asker=None,
    )
    skill = agent.store.get("evil")
    assert skill.shadow is not None and "evil" in agent.store.untrusted

    seen = []

    async def candidates(task, skills):
        seen.extend(s.id for s in skills)
        return []

    agent.matcher.candidates = candidates
    out = await agent.evolver._shadow_match(SimpleNamespace(success=True, task="t"))
    assert out["status"] == "skipped" and seen == []  # 不可信的影子技能根本不进入候选
    with pytest.raises(RuntimeError, match="拒绝在 sandbox 档之外执行"):
        await agent.evolver._run_shadow(skill, 1, SimpleNamespace(task="t", trace={}), {})


# ── 真实的内核约束（Linux）──

PROBE = textwrap.dedent('''
    import importlib.util, json, os, socket, subprocess, sys, threading, zoneinfo
    spec = importlib.util.spec_from_file_location("confine", {path!r})
    c = importlib.util.module_from_spec(spec); sys.modules["confine"] = c; spec.loader.exec_module(c)
    try:
        info = c.enter([sys.prefix, sys.base_prefix, *[p for p in sys.path if p], *zoneinfo.TZPATH])
    except c.ConfinementError as e:
        print(json.dumps({{"unsupported": str(e)}})); sys.exit(0)

    def attempt(fn):
        try:
            fn()
            return "allowed"
        except OSError as e:
            return type(e).__name__

    out = {{"abi": info.landlock_abi}}
    out["read_secret"] = attempt(lambda: open("/etc/passwd").read())
    out["write_tmp"] = attempt(lambda: open("/tmp/No-Agent-probe", "w").write("x"))
    out["socket"] = attempt(lambda: socket.socket())
    out["fork"] = attempt(os.fork)
    out["exec"] = attempt(lambda: subprocess.run(["/bin/true"]))
    out["signal_parent"] = attempt(lambda: os.kill(os.getppid(), 0))
    out["signal_self"] = attempt(lambda: os.kill(os.getpid(), 0))
    t = threading.Thread(target=lambda: None); t.start(); t.join(); out["thread"] = "allowed"
    out["stdlib_read"] = attempt(lambda: open(os.__file__).read(10))
    out["zoneinfo"] = str(zoneinfo.ZoneInfo("Asia/Shanghai"))
    print(json.dumps(out))
''')


@pytest.mark.skipif(not LINUX, reason="内核约束只在 Linux 上生效")
def test_kernel_confinement_blocks_escaped_code():
    """假设脚本已经完全逃逸 Python 层沙箱：直接在受约束的进程里执行任意代码"""
    import json

    code = PROBE.format(path=str(Path(c.__file__)))
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if "unsupported" in out:
        pytest.fail(f"这台 Linux 不支持 sandbox 档：{out['unsupported']}")
    assert out["read_secret"] == "PermissionError"
    assert out["write_tmp"] == "PermissionError"
    assert out["socket"] == "PermissionError"
    assert out["fork"] == "PermissionError"
    assert out["exec"] in ("PermissionError", "BlockingIOError")
    assert out["signal_parent"] == "PermissionError"
    assert (out["signal_self"], out["thread"], out["stdlib_read"]) == ("allowed", "allowed", "allowed")
    assert out["zoneinfo"] == "Asia/Shanghai"


@pytest.mark.skipif(not LINUX, reason="内核约束只在 Linux 上生效")
async def test_scripts_run_normally_in_sandbox():
    runner = ProcessRunner(WorkerPool(0, 512, 60, sandbox=True))
    src = '''
import datetime
import json
import zoneinfo

@step
async def a(ctx):
    r = await ctx.tool("echo", text="ok")
    tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    return {"r": r, "tz": str(datetime.datetime(2026, 1, 1, tzinfo=tz).utcoffset()), "j": json.dumps([1])}
'''
    res = await execute_script(src, task="t", params={}, toolbox=_box(), llm=NoLLM(), runner=runner)
    assert res.success, res.error
    assert res.result == {"r": "ok", "tz": "8:00:00", "j": "[1]"}
    assert os.path.exists(sys.prefix)
