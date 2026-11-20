"""
ProcessRunner：在一次性子进程（worker）中执行技能脚本

- 宿主只做编排与代办：工具、LLM、子技能调用、权限检查、密钥都不进入 worker
- worker 用一次就丢；预热池里放的是已启动、尚未加载任何脚本的空进程
- 只用 spawn（全新解释器、最小环境变量、空临时目录），绝不 fork：fork 会把宿主内存（含 API Key）复制过去
- 只用 JSON 帧，绝不 pickle：反序列化 worker 发来的 pickle 等于让它在宿主执行代码
- 超时 / 取消 → 直接终止进程；Windows 上用 Job Object 限制内存、禁止创建子进程，句柄关闭即终止

它不阻止脚本（在逃逸 Python 层沙箱之后）读写当前用户能访问的文件或直接联网，见 docs/design/script-isolation.html。
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any

from no_agent.skills._worker import MAX_FRAME, ProtocolError, encode_frame, read_frame
from no_agent.skills.sandbox import Phase, PhaseOutcome, ScriptError, Snapshot, StepFailure, StepMeta, step_meta, validate_script
from no_agent.tools.base import ToolError, UserAbort, to_jsonable

logger = logging.getLogger(__name__)

WORKER_PATH = Path(__file__).resolve().with_name("_worker.py")
STARTUP_TIMEOUT = 30.0
STDERR_LINES = 200


class WorkerError(Exception):
    """worker 异常退出或违反协议（不是脚本逻辑的错）"""


class WorkerStartError(RuntimeError):
    """无法启动 worker（环境问题，不应被当作脚本错误去修补）"""


# ── Windows Job Object ──


def _windows_job(proc: subprocess.Popen, memory_mb: int, cpu_s: int) -> Any:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):  # noqa: N801
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObject 失败")
    info = EXTENDED()
    flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS
    info.BasicLimitInformation.ActiveProcessLimit = 1  # 不能再创建子进程
    if memory_mb > 0:
        flags |= 0x100  # PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
    if cpu_s > 0:
        flags |= 0x2  # PROCESS_TIME（100 纳秒为单位）
        info.BasicLimitInformation.PerProcessUserTimeLimit = cpu_s * 10_000_000
    info.BasicLimitInformation.LimitFlags = flags
    ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) and k32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
    )
    if not ok:
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise OSError(err, "设置 Job Object 失败")
    return (k32, job)


def _minimal_env(tmp: str) -> dict[str, str]:
    if os.name == "nt":
        env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ}
        env.update(TEMP=tmp, TMP=tmp)
        return env
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": tmp, "TMPDIR": tmp}


# ── 单个 worker 进程 ──


class WorkerProcess:
    """
    一个 worker 子进程。帧由后台线程读出，推送给当前绑定的事件循环——
    进程本身不属于任何事件循环，可以跨循环 / 线程放在池里。
    """

    def __init__(self, memory_mb: int, cpu_s: int, sandbox: bool = False) -> None:
        self.tmp = tempfile.mkdtemp(prefix="No-Agent-worker-")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
        mode = "sandbox" if sandbox else "process"
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", str(WORKER_PATH), str(memory_mb), str(cpu_s), mode],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.tmp, env=_minimal_env(self.tmp), creationflags=flags, close_fds=True,
                start_new_session=os.name == "posix",  # 独立的进程组：kill(0, …) 打不到宿主
            )
        except OSError as e:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise WorkerStartError(f"无法启动脚本 worker: {e}") from e
        self._job: Any = None
        if os.name == "nt":
            try:
                self._job = _windows_job(self.proc, memory_mb, cpu_s)
            except OSError as e:
                logger.warning("无法为脚本 worker 设置资源限制（Job Object）: %s", e)
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._lock = threading.Lock()
        self._buffer: list[tuple[str, Any]] = []
        self._sink: tuple[asyncio.AbstractEventLoop, asyncio.Queue] | None = None
        self.dead = False
        threading.Thread(target=self._read_loop, name="No-Agent-worker-out", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="No-Agent-worker-err", daemon=True).start()

    # 后台线程

    def _push(self, item: tuple[str, Any]) -> None:
        with self._lock:
            if self._sink is None:
                self._buffer.append(item)
                return
            loop, q = self._sink
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
        except RuntimeError:  # 事件循环已关闭
            pass

    def _read_loop(self) -> None:
        assert self.proc.stdout
        while True:
            try:
                msg = read_frame(self.proc.stdout)
            except ProtocolError as e:
                self._push(("error", f"worker 违反协议: {e}"))
                self.kill()
                return
            except (OSError, ValueError):
                msg = None
            if msg is None:
                self._push(("eof", None))
                return
            self._push(("msg", msg))

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            self.stderr.append(line[:2000])
            logger.debug("[script worker] %s", line)

    # 事件循环侧

    def bind(self) -> None:
        """把后续的帧推送到当前事件循环"""
        loop, q = asyncio.get_running_loop(), asyncio.Queue()
        with self._lock:
            for item in self._buffer:
                q.put_nowait(item)
            self._buffer.clear()
            self._sink = (loop, q)

    async def recv(self) -> dict[str, Any]:
        assert self._sink is not None
        kind, value = await self._sink[1].get()
        if kind == "msg":
            return value
        self.kill()
        if kind == "error":
            raise WorkerError(value)
        code = await asyncio.to_thread(self.proc.wait)
        tail = "\n".join(list(self.stderr)[-5:])
        raise WorkerError(f"worker 异常退出（退出码 {code}）" + (f": {tail}" if tail else ""))

    async def send(self, obj: dict[str, Any]) -> None:
        try:
            data = encode_frame(obj)
        except ProtocolError as e:
            raise WorkerError(str(e)) from None
        # 写入放到线程里：一个不读 stdin 的 worker 只会卡住这个线程，卡不住事件循环；超时后进程被终止，写入随之失败
        try:
            await asyncio.to_thread(self._write, data)
        except (OSError, ValueError) as e:
            self.kill()
            raise WorkerError(f"无法向 worker 发送消息: {e}") from e

    def _write(self, data: bytes) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def alive(self) -> bool:
        return not self.dead and self.proc.poll() is None

    def kill(self) -> None:
        with self._lock:
            if self.dead:
                return
            self.dead = True
        try:
            if self._job is not None:
                k32, job = self._job
                k32.CloseHandle(job)  # KILL_ON_JOB_CLOSE
            self.proc.kill()
        except OSError:
            pass
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── 预热池 ──


class WorkerPool:
    def __init__(self, size: int, memory_mb: int, cpu_s: int, sandbox: bool = False) -> None:
        self.size = size
        self.memory_mb = memory_mb
        self.cpu_s = cpu_s
        self.sandbox = sandbox
        self._idle: deque[WorkerProcess] = deque()
        self._lock = threading.Lock()
        self._refilling = False
        self._closed = False

    def _spawn(self) -> WorkerProcess:
        return WorkerProcess(self.memory_mb, self.cpu_s, self.sandbox)

    def acquire(self) -> WorkerProcess:
        """取一个空闲 worker（没有就现场启动），并在后台补满预热池。阻塞调用，请放在线程里"""
        w = None
        with self._lock:
            while self._idle:
                cand = self._idle.popleft()
                if cand.alive():
                    w = cand
                    break
                cand.kill()
        if w is None:
            w = self._spawn()
        self._refill()
        return w

    def _refill(self) -> None:
        with self._lock:
            if self._refilling or self._closed or len(self._idle) >= self.size:
                return
            self._refilling = True

        def run() -> None:
            try:
                while True:
                    with self._lock:
                        if self._closed or len(self._idle) >= self.size:
                            return
                    try:
                        w = self._spawn()
                    except WorkerStartError as e:
                        logger.warning("%s", e)
                        return
                    with self._lock:
                        if self._closed:
                            w.kill()
                            return
                        self._idle.append(w)
            finally:
                with self._lock:
                    self._refilling = False

        threading.Thread(target=run, name="No-Agent-worker-pool", daemon=True).start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = list(self._idle), deque()
        for w in idle:
            w.kill()


_POOLS: dict[tuple[int, int, int, bool], WorkerPool] = {}
_POOLS_LOCK = threading.Lock()


def get_pool(size: int, memory_mb: int, cpu_s: int, sandbox: bool = False) -> WorkerPool:
    """同样配置共用一个池（多个 Agent 实例也一样）"""
    key = (size, memory_mb, cpu_s, sandbox)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None or pool._closed:
            pool = _POOLS[key] = WorkerPool(size, memory_mb, cpu_s, sandbox)
        return pool


@atexit.register
def _close_pools() -> None:
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    for p in pools:
        p.close()


# ── Runner / Session ──


def _snapshot(raw: Any, fallback: Snapshot) -> Snapshot:
    if not isinstance(raw, dict):
        return fallback
    state = raw.get("state")
    logs = raw.get("logs")
    return Snapshot(
        result=raw.get("result"),
        has_result=bool(raw.get("has_result")),
        state=state if isinstance(state, dict) else {},
        logs=[str(x) for x in logs[-1000:]] if isinstance(logs, list) else [],
    )


_STATUSES = {"ok", "rejected", "fail", "tool_error", "abort", "timeout", "error", "no_result"}


class ProcessSession:
    def __init__(self, worker: WorkerProcess, bridge: Any, steps: list[StepMeta]) -> None:
        self._w = worker
        self._bridge = bridge
        self.steps = steps
        self._snap = Snapshot()

    async def run(self, phase: Phase, name: str) -> PhaseOutcome:
        if not self._w.alive():
            return PhaseOutcome("error", "worker 已退出")
        try:
            await self._w.send({"op": "run", "phase": phase, "name": name})
            while True:
                msg = await self._w.recv()
                if msg["op"] == "call":
                    await self._serve(msg)
                    continue
                if msg["op"] != "done":
                    raise WorkerError(f"意外的消息 {msg['op']}")
                self._snap = _snapshot(msg.get("snapshot"), self._snap)
                status = msg.get("status")
                if status not in _STATUSES:
                    raise WorkerError(f"未知的阶段状态 {status!r}")
                return PhaseOutcome(status, str(msg.get("message", "")))
        except asyncio.CancelledError:
            self._w.kill()  # 宿主判定超时：直接终止，快照保留上一个阶段结束时的状态
            raise
        except WorkerError as e:
            self._w.kill()
            return PhaseOutcome("error", str(e))

    async def _serve(self, msg: dict[str, Any]) -> None:
        call_id, kind = msg.get("id"), msg.get("kind")
        name, args = msg.get("name"), msg.get("args")
        try:
            if kind == "tool" and isinstance(name, str) and isinstance(args, dict):
                value = await self._bridge.tool(name, args)
            elif kind == "skill" and isinstance(name, str) and isinstance(args, dict):
                value = await self._bridge.skill(name, args)
            elif kind == "llm" and isinstance(msg.get("instruction"), str):
                value = await self._bridge.llm(msg["instruction"], msg.get("input"), bool(msg.get("json")))
            else:
                raise WorkerError(f"无效的请求: {kind!r}")
            reply: dict[str, Any] = {"op": "reply", "id": call_id, "ok": True, "value": to_jsonable(value)}
        except WorkerError:
            raise
        except StepFailure as e:
            reply = self._err(call_id, "fail", e)
        except UserAbort as e:
            reply = self._err(call_id, "abort", e)
        except ToolError as e:
            reply = self._err(call_id, "tool_error", e)
        except Exception as e:  # noqa: BLE001 - 宿主侧的其他异常原样（类型名 + 信息）交给脚本，与进程内执行一致
            reply = self._err(call_id, "error", e)
        try:
            encode_frame(reply)
        except ProtocolError:  # 输出超过帧上限：告诉脚本这次调用失败，而不是让整个执行崩溃
            reply = self._err(call_id, "tool_error", f"{kind} 的输出过大（上限 {MAX_FRAME} 字节）")
        await self._w.send(reply)

    @staticmethod
    def _err(call_id: Any, kind: str, e: Any) -> dict[str, Any]:
        type_name = type(e).__name__ if isinstance(e, BaseException) else "Exception"
        return {"op": "reply", "id": call_id, "ok": False, "error": {"kind": kind, "type": type_name, "message": str(e)}}

    def snapshot(self) -> Snapshot:
        return self._snap

    async def close(self) -> None:
        self._w.kill()  # 用一次就丢


class ProcessRunner:
    """在一次性子进程中执行脚本"""

    def __init__(self, pool: WorkerPool) -> None:
        self.pool = pool

    @property
    def sandboxed(self) -> bool:
        """worker 是否处于操作系统级约束之下（sandbox 档）"""
        return self.pool.sandbox

    async def open(self, source: str, *, task: str, params: dict[str, Any], bridge: Any) -> ProcessSession:
        validate_script(source)  # 先在宿主静态校验：不合法的脚本不必启动进程
        steps = step_meta(source)
        w = await asyncio.to_thread(self.pool.acquire)
        try:
            await asyncio.wait_for(self._handshake(w, source, task, params, steps), timeout=STARTUP_TIMEOUT)
        except ScriptError:
            w.kill()
            raise
        except asyncio.TimeoutError:
            w.kill()
            raise WorkerStartError(f"脚本 worker 在 {STARTUP_TIMEOUT:.0f}s 内没有就绪") from None
        except WorkerError as e:
            w.kill()
            raise WorkerStartError(str(e)) from e
        except BaseException:
            w.kill()
            raise
        return ProcessSession(w, bridge, steps)

    async def _handshake(self, w: WorkerProcess, source: str, task: str, params: dict[str, Any], steps: list[StepMeta]) -> None:
        w.bind()
        msg = await w.recv()
        if msg["op"] == "failed" and msg.get("kind") == "sandbox":
            raise WorkerError(f"worker 无法进入 sandbox: {msg.get('message', '')}")
        if msg["op"] != "ready":
            raise WorkerError(f"意外的消息 {msg['op']}")
        if self.sandboxed and not isinstance(msg.get("confinement"), dict):
            raise WorkerError("worker 没有报告已进入 sandbox")
        await w.send({"op": "load", "source": source, "task": task, "params": to_jsonable(params)})
        msg = await w.recv()
        if msg["op"] == "failed":
            raise ScriptError(str(msg.get("message", "加载失败")))
        if msg["op"] != "loaded":
            raise WorkerError(f"意外的消息 {msg['op']}")
        if msg.get("steps") != [s.name for s in steps]:
            raise ScriptError("步骤列表与静态解析不一致（步骤必须是顶层的 @step async def）")
