"""The wall around the console's web process: kernel rules it cannot lift.

The console is the most exposed process on the box, and it runs as the same user that holds the
trading keys. systemd's ``InaccessiblePaths=`` does nothing in a user unit on this box (it needs an
unprivileged user namespace, which AppArmor refuses) and fails silently, so the first wall here
never existed. This one uses two mechanisms that need no root and no namespace:

* **Landlock** limits which files the process may open and which TCP ports it may use. The rules
  follow the real file, so ``/proc/<pid>/root/...`` does not get round them, and Landlock also
  refuses ptrace of anything outside the wall, which closes ``/proc/<pid>/environ`` and ``mem``.
* **seccomp** closes what Landlock does not cover. Landlock cannot stop a connect to a named unix
  socket, and the user manager's socket is a shell: anything that reaches it can start a program
  outside the wall. So no unix socket can be created, and io_uring is refused because it can
  create one without calling ``socket``. Signals to other processes are refused as well.

Both are inherited by every child and cannot be undone. :func:`prove` tries each thing the wall
claims to stop and refuses to serve if any of them works, so this cannot fail silently too.
Everything the console legitimately needs from outside goes through :mod:`dashboard.broker`.
"""

from __future__ import annotations

import ctypes
import errno
import os
import socket
import struct
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_RULE_NET_PORT = 2
_NET_BIND_TCP = 1
_NET_CONNECT_TCP = 2
#: TCP rules arrived in ABI 4. A lower ABI could not stop the web process reaching local services.
MIN_ABI = 4

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_DIR, MAKE_REG = 1 << 4, 1 << 5, 1 << 7, 1 << 8
TRUNCATE = 1 << 14
#: Every right ABI 4 knows. Handling them all means anything without a rule is refused.
_ALL_FS = (1 << 15) - 1
#: The only rights Landlock accepts on a rule for a single file.
_FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE

READ = READ_FILE | READ_DIR
WRITE = READ | WRITE_FILE | TRUNCATE
OWN_DIR = WRITE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG

_PR_SET_PDEATHSIG = 1
_PR_SET_SECCOMP = 22
_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_MODE_FILTER = 2
_AUDIT_ARCH_X86_64 = 0xC000003E
_NR_SOCKET, _NR_SOCKETPAIR, _NR_KILL, _NR_TKILL, _NR_TGKILL = 41, 53, 62, 200, 234
_NR_IO_URING_SETUP, _NR_IO_URING_ENTER, _NR_IO_URING_REGISTER = 425, 426, 427
_X32_SYSCALL_BIT = 0x40000000
_LD_ABS, _JEQ, _JGE, _AND, _RET = 0x20, 0x15, 0x35, 0x54, 0x06
_SECCOMP_DATA_NR, _SECCOMP_DATA_ARCH, _SECCOMP_DATA_ARG0, _SECCOMP_DATA_ARG1 = 0, 4, 16, 24
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_DENY = 0x00050000 | errno.EPERM


class WallError(RuntimeError):
    """The wall could not be built or does not hold. The console must not serve."""


@dataclass(frozen=True)
class Policy:
    """What the walled process may touch. Anything not listed is refused."""

    read: tuple[str, ...] = ()
    #: Existing files it may rewrite in place, by inode.
    write: tuple[str, ...] = ()
    #: Directories it owns outright.
    own: tuple[str, ...] = ()
    #: Listed files that are allowed to be missing when the wall goes up.
    optional: frozenset[str] = field(default_factory=frozenset)
    bind_ports: tuple[int, ...] = ()
    #: Existing files that must be refused. :func:`prove` tries each one and never reads it.
    forbidden: tuple[str, ...] = ()


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _check(result: int, what: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise WallError(f"{what}: {errno.errorcode.get(code, code)}")
    return result


def landlock_abi() -> int:
    """The kernel's Landlock ABI, or 0 if it has none."""
    version = _libc().syscall(_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                              ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return max(int(version), 0)


def _add_path(libc: ctypes.CDLL, ruleset: int, path: str, rights: int, optional: bool) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        if optional:
            return
        raise WallError(f"wall rule for a missing path: {path}") from None
    try:
        if not Path(path).is_dir():
            rights &= _FILE_RIGHTS
        attr = ctypes.create_string_buffer(struct.pack("<Qi", rights, fd), 12)
        _check(libc.syscall(_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
                            attr, ctypes.c_uint32(0)), f"landlock rule for {path}")
    finally:
        os.close(fd)


def _add_port(libc: ctypes.CDLL, ruleset: int, port: int) -> None:
    attr = ctypes.create_string_buffer(struct.pack("<QQ", _NET_BIND_TCP, port), 16)
    _check(libc.syscall(_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_NET_PORT),
                        attr, ctypes.c_uint32(0)), f"landlock rule for port {port}")


def _landlock(policy: Policy) -> None:
    abi = landlock_abi()
    if abi < MIN_ABI:
        raise WallError(f"kernel Landlock ABI {abi}, need {MIN_ABI}")
    libc = _libc()
    attr = ctypes.create_string_buffer(struct.pack("<QQ", _ALL_FS, _NET_BIND_TCP | _NET_CONNECT_TCP), 16)
    ruleset = _check(libc.syscall(_LANDLOCK_CREATE_RULESET, attr, ctypes.c_size_t(16), ctypes.c_uint32(0)),
                     "landlock ruleset")
    try:
        groups: tuple[tuple[Iterable[str], int], ...] = (
            (policy.read, READ), (policy.write, WRITE), (policy.own, OWN_DIR))
        for paths, rights in groups:
            for path in paths:
                _add_path(libc, ruleset, path, rights, path in policy.optional)
        for port in policy.bind_ports:
            _add_port(libc, ruleset, port)
        _check(libc.syscall(_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
               "landlock restrict")
    finally:
        os.close(ruleset)


def _assemble(program: list[tuple]) -> bytes:
    """Classic BPF with named jump targets. A step is (label | None, code, k, then, otherwise).

    BPF can only jump forwards, so every target has to sit below every step that uses it.
    """
    labels = {step[0]: i for i, step in enumerate(program) if step[0]}

    def offset(target: str | None, here: int) -> int:
        if target is None:
            return 0
        if labels[target] <= here:
            raise WallError(f"seccomp step {here} jumps backwards to {target}")
        return labels[target] - here - 1

    out = b""
    for i, (_label, code, k, then, otherwise) in enumerate(program):
        out += struct.pack("<HBBI", code, offset(then, i), offset(otherwise, i), k)
    return out


def _filter(pid: int) -> bytes:
    return _assemble([
        (None, _LD_ABS, _SECCOMP_DATA_ARCH, None, None),
        (None, _JEQ, _AUDIT_ARCH_X86_64, None, "kill"),
        (None, _LD_ABS, _SECCOMP_DATA_NR, None, None),
        (None, _JGE, _X32_SYSCALL_BIT, "deny", None),
        (None, _JEQ, _NR_SOCKET, "socket", None),
        (None, _JEQ, _NR_SOCKETPAIR, "stream", None),
        (None, _JEQ, _NR_IO_URING_SETUP, "deny", None),
        (None, _JEQ, _NR_IO_URING_ENTER, "deny", None),
        (None, _JEQ, _NR_IO_URING_REGISTER, "deny", None),
        (None, _JEQ, _NR_TKILL, "deny", None),
        (None, _JEQ, _NR_KILL, "self", None),
        (None, _JEQ, _NR_TGKILL, "self", None),
        (None, _JGE, 0, "allow", "allow"),
        ("socket", _LD_ABS, _SECCOMP_DATA_ARG0, None, None),
        (None, _JEQ, int(socket.AF_INET), "stream", None),
        (None, _JEQ, int(socket.AF_INET6), "stream", "deny"),
        ("stream", _LD_ABS, _SECCOMP_DATA_ARG1, None, None),
        (None, _AND, 0xF, None, None),
        (None, _JEQ, int(socket.SOCK_STREAM), "allow", "deny"),
        ("self", _LD_ABS, _SECCOMP_DATA_ARG0, None, None),
        (None, _JEQ, pid, "allow", "deny"),
        ("deny", _RET, _RET_DENY, None, None),
        ("kill", _RET, _RET_KILL_PROCESS, None, None),
        ("allow", _RET, _RET_ALLOW, None, None),
    ])


def _seccomp() -> None:
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise WallError(f"no seccomp filter for {sys.platform} {os.uname().machine}")
    code = _filter(os.getpid())
    libc = _libc()
    buffer = ctypes.create_string_buffer(code, len(code))
    prog = struct.pack("<H6xQ", len(code) // 8, ctypes.addressof(buffer))
    prog_buffer = ctypes.create_string_buffer(prog, len(prog))
    libc.prctl.restype = ctypes.c_int
    _check(libc.prctl(ctypes.c_int(_PR_SET_SECCOMP), ctypes.c_ulong(_SECCOMP_MODE_FILTER),
                      prog_buffer, ctypes.c_ulong(0), ctypes.c_ulong(0)), "seccomp filter")


def die_with_parent(sig: int) -> None:
    """Have the kernel send ``sig`` here when the process that forked this one exits."""
    _check(_libc().prctl(ctypes.c_int(_PR_SET_PDEATHSIG), ctypes.c_ulong(sig), ctypes.c_ulong(0),
                         ctypes.c_ulong(0), ctypes.c_ulong(0)), "parent death signal")


def apply(policy: Policy) -> None:
    """Build the wall around this process. Permanent: nothing here can be undone."""
    libc = _libc()
    _check(libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0),
                      ctypes.c_ulong(0), ctypes.c_ulong(0)), "no new privileges")
    _landlock(policy)
    _seccomp()


def prove(policy: Policy) -> None:
    """Try what the wall claims to stop. Any success raises, so the console never serves unwalled."""
    for path in policy.forbidden:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except PermissionError:
            continue
        except OSError as exc:
            raise WallError(f"cannot test {path}: {exc}") from exc
        os.close(fd)
        raise WallError(f"the wall did not stop opening {path}")
    try:
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except PermissionError:
        pass
    else:
        unix.close()
        raise WallError("the wall did not stop creating a unix socket")
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        tcp.settimeout(2)
        tcp.connect(("127.0.0.1", 9))
    except PermissionError:
        pass
    except OSError as exc:
        raise WallError(f"the wall did not stop a TCP connect ({exc})") from exc
    else:
        raise WallError("the wall did not stop a TCP connect")
    finally:
        tcp.close()
    try:
        subprocess.run(["/bin/true"], check=False)
    except PermissionError:
        pass
    else:
        raise WallError("the wall did not stop starting a program")
