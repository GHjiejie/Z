"""Irreversible Linux parser policy. Call only inside a disposable interpreter.

Landlock constrains filesystem content access; seccomp defaults to EPERM and
allows only the single-threaded parser's IO/memory/runtime calls. No document
bytes may be consumed before enforce() returns. Python audit hooks are not the
security boundary. The worker's numerical UID is retained, but its Landlock
domain and syscall rights are strictly less privileged than the parent.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from pathlib import Path
import platform
import re
import socket
import sys
import sysconfig


POLICY_VERSION = "landlock-seccomp-v1"
MIN_LANDLOCK_ABI = 3  # Requires REFER and TRUNCATE, not just read/write paths.
ALLOW = 0x7FFF0000
DENY = 0x00050000 | errno.EPERM
KILL_PROCESS = 0x80000000
READ_FILE = 1 << 2
READ_DIR = 1 << 3

# Deliberately no socket/IPC, exec, clone, ptrace, process_vm, pidfd, signals to
# other processes, namespace/mount, ioctl, io_uring, prctl or resource changes.
# Unknown/new syscalls remain denied. Alternate syscall ABIs are not enabled.
SYSCALLS = (
    "read", "readv", "pread64", "write", "writev", "close", "lseek",
    "open", "openat", "stat", "lstat", "fstat", "newfstatat", "statx",
    "getdents", "getdents64", "readlink", "readlinkat", "access", "faccessat", "faccessat2",
    "mmap", "mprotect", "munmap", "mremap", "brk", "madvise",
    "futex", "futex_time64", "clock_gettime", "clock_gettime64", "gettimeofday", "time",
    "clock_getres", "nanosleep", "clock_nanosleep", "restart_syscall",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack",
    "getpid", "getppid", "gettid", "getuid", "geteuid", "getgid", "getegid",
    "getrandom", "getcwd", "uname", "getrusage", "sched_getaffinity", "sched_yield",
    "exit", "exit_group",
)


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_ = 1  # Linux UAPI is packed: __u64 allowed_access; __s32 parent_fd.
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class ArgCompare(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


class KernelPolicy:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long
        self.libc.prctl.restype = ctypes.c_int
        self.libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        # Exact SONAME; do not execute ldconfig/compiler helpers to find a library.
        self.seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        self.seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        self.seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        self.seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        self.seccomp.seccomp_init.restype = ctypes.c_void_p
        self.seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCompare)]
        self.seccomp.seccomp_rule_add_array.restype = ctypes.c_int
        self.seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]
        self.seccomp.seccomp_attr_set.restype = ctypes.c_int
        self.seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        self.seccomp.seccomp_load.restype = ctypes.c_int
        self.seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        self.seccomp.seccomp_release.restype = None

    def syscall(self, name, *args):
        number = self.seccomp.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            raise RuntimeError("Required sandbox syscall is unavailable")
        result = self.libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            raise RuntimeError("Kernel refused parser filesystem isolation")
        return result

    def restrict_privileges(self):
        for option, value in ((38, 1), (4, 0)):  # NO_NEW_PRIVS, non-dumpable.
            if self.libc.prctl(option, value, 0, 0, 0) != 0:
                raise RuntimeError("Kernel refused parser privilege restriction")

    def restrict_files(self, paths):
        abi = self.syscall("landlock_create_ruleset", ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
        if abi < MIN_LANDLOCK_ABI:
            raise RuntimeError("Parser requires Landlock ABI 3 or newer")
        # ABI 3 handles bits 0..14, ABI 5 adds IOCTL_DEV. Seccomp independently
        # denies ioctl and all socket calls even on earlier supported ABIs.
        attributes = Ruleset((1 << (16 if abi >= 5 else 15)) - 1)
        ruleset = self.syscall("landlock_create_ruleset", ctypes.byref(attributes),
                               ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0))
        try:
            for path in paths:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
                try:
                    rule = PathRule(READ_FILE | (READ_DIR if path.is_dir() else 0), descriptor)
                    self.syscall("landlock_add_rule", ctypes.c_int(ruleset), ctypes.c_int(1),
                                 ctypes.byref(rule), ctypes.c_uint(0))
                finally:
                    os.close(descriptor)
            self.syscall("landlock_restrict_self", ctypes.c_int(ruleset), ctypes.c_uint(0))
        finally:
            os.close(ruleset)
        return abi

    def restrict_syscalls(self):
        library = self.seccomp
        context = library.seccomp_init(DENY)
        if not context:
            raise RuntimeError("Cannot allocate parser syscall policy")
        try:
            # SCMP_FLTATR_ACT_BADARCH=2; SCMP_FLTATR_CTL_TSYNC=4.
            for attribute, value in ((2, KILL_PROCESS), (4, 1)):
                if library.seccomp_attr_set(context, attribute, value) != 0:
                    raise RuntimeError("Cannot configure parser syscall policy")
            for name in SYSCALLS:
                number = library.seccomp_syscall_resolve_name(name.encode())
                if number < 0:  # Architecture-specific optional runtime call.
                    continue
                if library.seccomp_rule_add_array(context, ALLOW, number, 0, None) != 0:
                    raise RuntimeError("Cannot build parser syscall policy")
            for name in ("fcntl", "fcntl64"):
                number = library.seccomp_syscall_resolve_name(name.encode())
                if number < 0:
                    continue
                # Read-only flag inspection, never F_SETOWN/F_SETSIG or locks.
                for command in (fcntl.F_GETFD, fcntl.F_GETFL):
                    argument = ArgCompare(1, 4, command, 0)  # SCMP_CMP_EQ.
                    if library.seccomp_rule_add_array(context, ALLOW, number, 1, ctypes.byref(argument)) != 0:
                        raise RuntimeError("Cannot restrict parser descriptor operations")
            if library.seccomp_load(context) != 0:
                raise RuntimeError("Kernel refused parser syscall isolation")
        finally:
            library.seccomp_release(context)


def runtime_read_paths():
    """Derive trusted runtime roots, never document- or environment-supplied paths.

    Project source is imported before isolation and is NOT granted filesystem
    access. Standard/site-package directories must be immutable release assets,
    without credentials or user-writable uploads. No /usr, /etc, /home, /proc,
    workspace, secret mount, temporary directory or arbitrary extra bind root.
    """
    paths = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = Path(sysconfig.get_path(key)).resolve(strict=True)
        if (not path.is_dir() or not re.search(r"/python3(?:\.\d+)?(?:/(?:site|dist)-packages)?$", str(path))):
            raise RuntimeError("Unsupported parser runtime library layout")
        paths.add(path)
    for entry in sys.path:
        path = Path(entry)
        if re.fullmatch(r"python\d+\.zip", path.name) and path.is_file():
            resolved = path.resolve(strict=True)
            if resolved.parent not in {item.parent for item in paths}:
                raise RuntimeError("Unsupported parser standard library archive")
            paths.add(resolved)
    return sorted(paths)


def _verify_denials():
    # Use synthetic/own-process probes, never inspect a real credential file.
    for path, flags in (("/proc/self/environ", os.O_RDONLY),
                        ("parser-write-probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL)):
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EPERM):
                raise RuntimeError("Cannot verify parser filesystem restriction") from error
        else:
            os.close(descriptor)
            raise RuntimeError("Parser filesystem restriction did not take effect")
    try:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        if error.errno != errno.EPERM:
            raise RuntimeError("Cannot verify parser network restriction") from error
    else:
        connection.close()
        raise RuntimeError("Parser network restriction did not take effect")


def _check_identity():
    if not sys.platform.startswith("linux") or platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("Parser OS isolation requires supported Linux architecture")
    if os.geteuid() == 0 or os.getuid() != os.geteuid() or os.getgid() != os.getegid():
        raise RuntimeError("Parser OS isolation requires an unprivileged identity")


def _close_extra_fds():
    # Landlock does not revoke already-open descriptions. Imports must not leave
    # a file/socket handle that would survive policy installation.
    for entry in os.listdir('/proc/self/fd'):
        descriptor = int(entry)
        if descriptor > 2:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:  # listdir's own descriptor is gone.
                    raise


def enforce():
    _check_identity()
    # Load all trusted native parser libraries before denying new filesystem
    # access. These imports receive no document content or business credentials.
    import docx  # noqa: F401
    import pypdf  # noqa: F401
    paths = runtime_read_paths()
    kernel = KernelPolicy()
    if len(os.listdir("/proc/self/task")) != 1:
        raise RuntimeError("Parser must be single-threaded before filesystem isolation")
    _close_extra_fds()
    kernel.restrict_privileges()
    abi = kernel.restrict_files(paths)
    kernel.restrict_syscalls()
    _verify_denials()
    return {"mode": POLICY_VERSION, "landlock_abi": abi}
