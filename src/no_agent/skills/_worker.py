"""
脚本 worker 子进程入口（由 ProcessRunner 启动，一个进程只服务一次脚本执行）

    python -I -X utf8 _worker.py <内存上限 MB> <CPU 秒数上限>

只依赖标准库：按文件路径加载同目录的 sandbox.py，不 import no_agent 包（避免把框架、配置、密钥带进来）。
协议：stdin / stdout 上的帧（4 字节大端长度 + UTF-8 JSON）。脚本的 print 被重定向到 stderr，不会混进协议。

宿主 → worker：load{source, task, params} | run{phase, name} | reply{id, ok, value | error}
worker → 宿主：ready | loaded{steps} | failed{kind: load, message} | call{id, kind: tool|llm|skill, ...}
              | done{status, message, snapshot}

本文件同时被宿主 import（复用帧读写函数），模块顶层不能有副作用。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import struct
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO

MAX_FRAME = 8 * 1024 * 1024
RESULT_LIMIT = 4 * 1024 * 1024  # 快照里 result 的最大序列化长度，超出则截断（保证帧不超限）
_HDR = struct.Struct(">I")


class ProtocolError(BaseException):
    """协议错误。继承 BaseException：脚本里的 except Exception 吞不掉它"""


def encode_frame(obj: Any) -> bytes:
    data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError(f"消息过大（{len(data)} 字节，上限 {MAX_FRAME}）")
    return _HDR.pack(len(data)) + data


def write_frame(f: BinaryIO, obj: Any) -> None:
    f.write(encode_frame(obj))
    f.flush()


def _read_exact(f: BinaryIO, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            if buf:
                raise ProtocolError("消息不完整")
            return None
        buf += chunk
    return bytes(buf)


def read_frame(f: BinaryIO) -> dict[str, Any] | None:
    """读一帧；对端关闭时返回 None"""
    hdr = _read_exact(f, _HDR.size)
    if hdr is None:
        return None
    (n,) = _HDR.unpack(hdr)
    if n > MAX_FRAME:
        raise ProtocolError(f"消息过大（{n} 字节）")
    data = _read_exact(f, n)
    if data is None:
        raise ProtocolError("消息不完整")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"消息不是合法的 JSON: {e}") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
        raise ProtocolError("消息格式错误")
    return obj


# ── 以下仅在 worker 进程中使用 ──


class WorkerToolError(Exception):
    """宿主的工具调用失败（对应 no_agent.tools.base.ToolError）"""


class WorkerAbort(Exception):
    """用户取消（对应 no_agent.tools.base.UserAbort）"""


def _apply_limits(memory_mb: int, cpu_s: int) -> None:
    """POSIX 上限制自身资源（Windows 由宿主用 Job Object 限制）。worker 自己的代码可信，在加载脚本之前执行"""
    if os.name != "posix":
        return
    import resource

    limits = [(getattr(resource, "RLIMIT_NPROC", None), 0)]  # 不能再创建进程（root 用户不受此限制）
    if memory_mb > 0:
        limits.append((resource.RLIMIT_AS, memory_mb * 1024 * 1024))
    if cpu_s > 0:
        limits.append((resource.RLIMIT_CPU, cpu_s))  # 兜底：宿主崩溃时，失控的脚本也会被内核终止
    for res, value in limits:
        if res is None:
            continue
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass 需要能按模块名找到自己
    spec.loader.exec_module(mod)
    return mod


class Worker:
    def __init__(self, sb: Any, inp: BinaryIO, out: BinaryIO) -> None:
        self.sb = sb
        self.inp = inp
        self.out = out
        self.loop = asyncio.new_event_loop()
        self.loaded: Any = None
        self.ctx: Any = None
        self.next_id = 0
        self.kinds = sb.ErrorKinds(tool_error=(WorkerToolError,), abort=(WorkerAbort,))
        self._error_types: dict[str, type[Exception]] = {}

    def send(self, obj: dict[str, Any]) -> None:
        write_frame(self.out, obj)

    def serve(self) -> None:
        while True:
            msg = read_frame(self.inp)
            if msg is None or msg["op"] == "close":
                return
            if msg["op"] == "load" and self.loaded is None:
                self._load(msg)
            elif msg["op"] == "run" and self.loaded is not None:
                self._run(msg)
            else:
                raise ProtocolError(f"意外的消息 {msg['op']}")

    def _load(self, msg: dict[str, Any]) -> None:
        try:
            self.loaded = self.sb.load_script(str(msg.get("source", "")))
        except self.sb.ScriptError as e:
            self.send({"op": "failed", "kind": "load", "message": str(e)})
            return
        params = msg.get("params")
        self.ctx = self.sb.ScriptContext(task=str(msg.get("task", "")), params=params if isinstance(params, dict) else {}, bridge=self)
        self.send({"op": "loaded", "steps": [s.name for s in self.loaded.steps]})

    def _run(self, msg: dict[str, Any]) -> None:
        out = self.loop.run_until_complete(
            self.sb.run_phase(self.loaded, self.ctx, msg.get("phase"), str(msg.get("name", "")), self.kinds)
        )
        snap = asdict(self.sb.snapshot(self.ctx))
        if len(json.dumps(snap["result"], ensure_ascii=False, default=str)) > RESULT_LIMIT:
            snap["result"] = self.sb.clip(snap["result"], RESULT_LIMIT)
        self.send({"op": "done", "status": out.status, "message": out.message, "snapshot": snap})

    # ── Bridge：ctx 的请求经协议转给宿主。脚本不能创建并发任务，所以请求总是一问一答 ──

    async def tool(self, name: str, args: dict[str, Any]) -> Any:
        return self._call("tool", name=name, args=args)

    async def llm(self, instruction: str, input: Any, json: bool) -> Any:  # noqa: A002
        return self._call("llm", instruction=instruction, input=input, json=json)

    async def skill(self, skill_id: str, params: dict[str, Any]) -> Any:
        return self._call("skill", name=skill_id, args=params)

    def _call(self, kind: str, **payload: Any) -> Any:
        self.next_id += 1
        self.send({"op": "call", "id": self.next_id, "kind": kind, **payload})
        msg = read_frame(self.inp)
        if msg is None:
            raise SystemExit(0)  # 宿主已经关闭
        if msg["op"] != "reply" or msg.get("id") != self.next_id:
            raise ProtocolError("回复与请求不匹配")
        if msg.get("ok"):
            return msg.get("value")
        err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
        raise self._error(str(err.get("kind", "")), str(err.get("type", "")), str(err.get("message", "")))

    def _error(self, kind: str, type_name: str, message: str) -> Exception:
        if kind == "fail":
            return self.sb.StepFailure(message)
        if kind == "tool_error":
            return WorkerToolError(message)
        if kind == "abort":
            return WorkerAbort(message)
        # 宿主侧的其他异常（如 LLM 调用失败）：保留类型名，脚本的报错信息与进程内执行一致
        name = type_name if type_name.isidentifier() else "Exception"
        if name not in self._error_types:
            self._error_types[name] = type(name, (Exception,), {})
        return self._error_types[name](message)


def main() -> None:
    memory_mb, cpu_s = (int(a) for a in sys.argv[1:3])
    _apply_limits(memory_mb, cpu_s)
    sb = _load_module("no_agent_worker_sandbox", Path(__file__).resolve().with_name("sandbox.py"))
    for m in sorted(sb.ALLOWED_IMPORTS | sb.ALLOWED_SUBMODULES):  # 预热：脚本里的 import 不再计入步骤耗时
        try:
            importlib.import_module(m)
        except Exception:  # noqa: BLE001
            pass

    inp, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # 脚本的 print 写到 stderr，协议只走原始 stdout
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115
    write_frame(out, {"op": "ready"})
    try:
        Worker(sb, inp, out).serve()
    except ProtocolError as e:
        print(f"[worker] 协议错误: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
