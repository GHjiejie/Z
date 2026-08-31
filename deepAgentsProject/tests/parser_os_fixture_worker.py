"""Native syscall probes in an owned child, using synthetic test files only."""
import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.knowledge.ingestion.limits import ParseLimits
from packages.knowledge.ingestion.worker import _bootstrap
from packages.knowledge.ingestion.linux_sandbox import enforce

_bootstrap({'limits': asdict(ParseLimits()), 'require_hard_memory': True})
libc = ctypes.CDLL(None, use_errno=True)
libc.open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
libc.open.restype = ctypes.c_int
libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
libc.socket.restype = ctypes.c_int
libc.kill.argtypes = [ctypes.c_int, ctypes.c_int]
libc.kill.restype = ctypes.c_int
libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
libc.prctl.restype = ctypes.c_int
libc.execve.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
libc.execve.restype = ctypes.c_int
policy = enforce()

# No Python audit hook is installed: all rejections must come from the kernel.
results = {}
for name, function, args in (
    ('outside_read', libc.open, (os.fsencode(sys.argv[1]), os.O_RDONLY, 0)),
    ('outside_write', libc.open, (os.fsencode(sys.argv[1] + '.write'), os.O_WRONLY | os.O_CREAT, 0o600)),
    ('ipv4', libc.socket, (socket.AF_INET, socket.SOCK_STREAM, 0)),
    ('ipv6', libc.socket, (socket.AF_INET6, socket.SOCK_STREAM, 0)),
    ('unix', libc.socket, (socket.AF_UNIX, socket.SOCK_STREAM, 0)),
    ('signals', libc.kill, (os.getpid(), 0)),  # Signal zero never delivers a signal.
    ('privilege_change', libc.prctl, (4, 1, 0, 0, 0)),
    ('exec', libc.execve, (b'/nonexistent-parser-test-program', None, None)),
):
    ctypes.set_errno(0)
    value = function(*args)
    code = ctypes.get_errno()
    results[name] = {'result': value, 'errno': code}
    if value >= 0 and name in {'outside_read', 'outside_write', 'ipv4', 'ipv6', 'unix'}:
        os.close(value)
print(json.dumps({'policy': policy, 'calls': results}))
