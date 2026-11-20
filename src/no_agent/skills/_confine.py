"""
sandbox 档的操作系统级约束（仅 Linux；只依赖标准库，由 worker 按文件路径加载）

worker 在预热完成、加载任何脚本之前调用 enter()，之后不可撤销：
- Landlock（内核 5.13+）：文件系统只读，且只能读 Python 安装目录和 sys.path；不能写任何地方
  （stdin / stdout / stderr 管道在此之前已打开，不受影响）
- seccomp：禁止创建 socket、执行程序、创建进程（只允许创建线程）、ptrace、
  给自己以外的进程发信号、io_uring 等可绕过约束的系统调用
任何一项不可用都抛 ConfinementError——由调用方拒绝启动，绝不静默降级。
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct
from dataclasses import dataclass


class ConfinementError(Exception):
    """当前环境无法施加约束"""


# ── seccomp ──

AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}

# 直接拒绝（EPERM）的系统调用
_DENY = {
    "x86_64": {
        "socket": 41, "connect": 42, "accept": 43, "bind": 49, "listen": 50, "socketpair": 53, "accept4": 288,
        "fork": 57, "vfork": 58, "execve": 59, "execveat": 322, "ptrace": 101, "tkill": 200,
        "process_vm_readv": 310, "process_vm_writev": 311, "pidfd_open": 434, "pidfd_send_signal": 424, "pidfd_getfd": 438,
        "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "unshare": 272, "setns": 308,
        "bpf": 321, "userfaultfd": 323, "perf_event_open": 298, "keyctl": 250, "add_key": 248, "request_key": 249,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
        "kexec_load": 246, "init_module": 175, "finit_module": 313, "delete_module": 176, "reboot": 169,
        "swapon": 167, "swapoff": 168, "iopl": 172, "ioperm": 173,
    },
    "aarch64": {
        "socket": 198, "connect": 203, "accept": 202, "bind": 200, "listen": 201, "socketpair": 199, "accept4": 242,
        "execve": 221, "execveat": 281, "ptrace": 117, "tkill": 130,
        "process_vm_readv": 270, "process_vm_writev": 271, "pidfd_open": 434, "pidfd_send_signal": 424, "pidfd_getfd": 438,
        "mount": 40, "umount2": 39, "pivot_root": 41, "chroot": 51, "unshare": 97, "setns": 268,
        "bpf": 280, "userfaultfd": 282, "perf_event_open": 241, "keyctl": 219, "add_key": 217, "request_key": 218,
        "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
        "kexec_load": 104, "init_module": 105, "finit_module": 273, "delete_module": 106, "reboot": 142,
        "swapon": 224, "swapoff": 225,
    },
}
# 需要检查参数的系统调用
_CLONE = {"x86_64": 56, "aarch64": 220}  # 只允许带 CLONE_THREAD（创建线程），不允许创建进程
_CLONE3 = 435  # 参数在结构体里，过滤器看不到 → 返回 ENOSYS，libc 会退回到 clone
# 只能给自己发信号（第一个参数是目标 pid / tgid）
_KILL = {
    "x86_64": {"kill": 62, "tgkill": 234, "rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297},
    "aarch64": {"kill": 129, "tgkill": 131, "rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240},
}

CLONE_THREAD = 0x00010000
EPERM, ENOSYS = 1, 38

# BPF 指令与 seccomp 返回值
_LD_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_JSET = 0x45  # BPF_JMP | BPF_JSET | BPF_K
_RET = 0x06  # BPF_RET | BPF_K
RET_ALLOW = 0x7FFF0000
RET_KILL_PROCESS = 0x80000000


def ret_errno(errno: int) -> int:
    return 0x00050000 | errno


# seccomp_data 中的偏移：nr 0，arch 4，args[i] 的低 32 位在 16 + 8i（小端）
_OFF_NR, _OFF_ARCH, _OFF_ARG0 = 0, 4, 16


def build_filter(arch: str, own_pid: int) -> list[tuple[int, int, int, int]]:
    """生成 seccomp BPF 程序：[(code, jt, jf, k)]。未列出的系统调用一律允许"""
    if arch not in AUDIT_ARCH:
        raise ConfinementError(f"不支持的 CPU 架构: {arch}")
    prog: list[tuple[int, int, int, int]] = [
        (_LD_ABS, 0, 0, _OFF_ARCH),
        (_JEQ, 1, 0, AUDIT_ARCH[arch]),
        (_RET, 0, 0, RET_KILL_PROCESS),  # 架构不符（例如 32 位兼容调用）→ 直接终止
        (_LD_ABS, 0, 0, _OFF_NR),
    ]
    if arch == "x86_64":  # x32 ABI 的系统调用号带 0x40000000 标记，可以绕过按号码的过滤
        prog += [(_JGE, 0, 1, 0x40000000), (_RET, 0, 0, RET_KILL_PROCESS)]
    for nr in sorted(_DENY[arch].values()):
        prog += [(_JEQ, 0, 1, nr), (_RET, 0, 0, ret_errno(EPERM))]
    prog += [(_JEQ, 0, 1, _CLONE3), (_RET, 0, 0, ret_errno(ENOSYS))]
    # clone：flags 带 CLONE_THREAD 才放行。每个块的所有分支都会返回，所以跳过整块时累加器里仍是系统调用号
    prog += [
        (_JEQ, 0, 4, _CLONE[arch]),
        (_LD_ABS, 0, 0, _OFF_ARG0),
        (_JSET, 0, 1, CLONE_THREAD),
        (_RET, 0, 0, RET_ALLOW),
        (_RET, 0, 0, ret_errno(EPERM)),
    ]
    # kill / tgkill：第一个参数必须是自己的 pid（kill(0, …) 会打到同一进程组里的宿主，同样拒绝）
    for nr in _KILL[arch].values():
        prog += [
            (_JEQ, 0, 4, nr),
            (_LD_ABS, 0, 0, _OFF_ARG0),
            (_JEQ, 0, 1, own_pid),
            (_RET, 0, 0, RET_ALLOW),
            (_RET, 0, 0, ret_errno(EPERM)),
        ]
    prog.append((_RET, 0, 0, RET_ALLOW))
    return prog


# ── Landlock ──

SYS_LANDLOCK_CREATE_RULESET, SYS_LANDLOCK_ADD_RULE, SYS_LANDLOCK_RESTRICT_SELF = 444, 445, 446  # 各架构统一
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 2, 1 << 3
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1


def handled_fs(abi: int) -> int:
    """该 ABI 下所有的文件系统访问权限（全部纳入管控，未授权的即被禁止）"""
    bits = 13  # v1：EXECUTE … MAKE_SYM
    if abi >= 2:
        bits = 14  # REFER
    if abi >= 3:
        bits = 15  # TRUNCATE
    if abi >= 5:
        bits = 16  # IOCTL_DEV
    return (1 << bits) - 1


def ruleset_attr(abi: int) -> bytes:
    """struct landlock_ruleset_attr，按 ABI 决定长度"""
    if abi >= 6:
        return struct.pack("=QQQ", handled_fs(abi), NET_BIND_TCP | NET_CONNECT_TCP, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL)
    if abi >= 4:
        return struct.pack("=QQ", handled_fs(abi), NET_BIND_TCP | NET_CONNECT_TCP)
    return struct.pack("=Q", handled_fs(abi))


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 38, 22, 2


@dataclass
class Confinement:
    landlock_abi: int
    seccomp: bool
    read_paths: list[str]


def _libc() -> ctypes.CDLL:
    if not os.uname().sysname == "Linux":  # type: ignore[attr-defined]
        raise ConfinementError("sandbox 档只支持 Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _check(ret: int, what: str) -> int:
    if ret < 0:
        err = ctypes.get_errno()
        raise ConfinementError(f"{what} 失败: {os.strerror(err)}（errno {err}）")
    return ret


def landlock_abi(libc: ctypes.CDLL | None = None) -> int:
    """内核支持的 Landlock ABI 版本；不支持返回 0"""
    libc = libc or _libc()
    abi = libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    return max(int(abi), 0)


def _landlock(libc: ctypes.CDLL, abi: int, read_paths: list[str]) -> None:
    attr = ruleset_attr(abi)
    buf = ctypes.create_string_buffer(attr, len(attr))
    fd = _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_CREATE_RULESET), buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0)), "landlock_create_ruleset")
    try:
        for path in read_paths:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)  # type: ignore[attr-defined]
            try:
                rights = FS_READ_FILE | FS_EXECUTE | (FS_READ_DIR if os.path.isdir(path) else 0)
                rule = _PathBeneath(allowed_access=rights, parent_fd=pfd)
                _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_ADD_RULE), ctypes.c_int(fd), ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule), ctypes.c_uint32(0)), f"landlock_add_rule({path})")
            finally:
                os.close(pfd)
        _check(libc.syscall(ctypes.c_long(SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(fd), ctypes.c_uint32(0)), "landlock_restrict_self")
    finally:
        os.close(fd)


def _seccomp(libc: ctypes.CDLL, prog: list[tuple[int, int, int, int]]) -> None:
    arr = (_SockFilter * len(prog))(*[_SockFilter(*ins) for ins in prog])
    fprog = _SockFprog(len(prog), ctypes.cast(arr, ctypes.POINTER(_SockFilter)))
    _check(libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0), "seccomp")


def enter(read_paths: list[str]) -> Confinement:
    """对当前进程施加约束（不可撤销）。read_paths：只读可访问的目录 / 文件"""
    libc = _libc()
    arch = platform.machine()
    prog = build_filter(arch, os.getpid())  # 先检查架构，避免施加一半约束后才发现不支持
    abi = landlock_abi(libc)
    if abi < 1:
        raise ConfinementError("内核不支持 Landlock（需要 Linux 5.13+ 且启用了 landlock LSM）")
    paths = sorted({os.path.realpath(p) for p in read_paths if os.path.exists(p)})
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "PR_SET_NO_NEW_PRIVS")
    _landlock(libc, abi, paths)
    _seccomp(libc, prog)
    return Confinement(landlock_abi=abi, seccomp=True, read_paths=paths)
