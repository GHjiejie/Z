from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from packages.knowledge.errors import ParseProtocolError, ParseUnavailable
from packages.knowledge.ingestion import linux_sandbox as sandbox
from packages.knowledge.ingestion.isolated import IsolatedDocumentParser
from packages.knowledge.service import KnowledgeService


def test_uapi_struct_layout_and_default_deny_contract():
    assert ctypes.sizeof(sandbox.Ruleset) == 8
    assert ctypes.sizeof(sandbox.PathRule) == 12
    assert sandbox.PathRule.parent_fd.offset == 8
    assert ctypes.sizeof(sandbox.ArgCompare) == 24
    assert sandbox.DENY == 0x00050001
    forbidden = {'socket', 'socketpair', 'connect', 'bind', 'sendto', 'sendmsg', 'recvmsg',
        'execve', 'execveat', 'clone', 'clone3', 'fork', 'vfork', 'kill', 'tgkill', 'tkill',
        'ptrace', 'process_vm_readv', 'process_vm_writev', 'pidfd_open', 'pidfd_getfd',
        'mount', 'unshare', 'setns', 'ioctl', 'io_uring_setup', 'bpf', 'prctl',
        'setrlimit', 'prlimit64', 'keyctl', 'setuid', 'setgid', 'fcntl', 'fcntl64'}
    assert not forbidden.intersection(sandbox.SYSCALLS)


@pytest.mark.parametrize('identity', [(0, 0, 0, 0), (1000, 1001, 1000, 1000), (1000, 1000, 1000, 1001)])
def test_root_and_setid_processes_are_rejected(monkeypatch, identity):
    with monkeypatch.context() as patch:
        patch.setattr(sandbox.sys, 'platform', 'linux')
        patch.setattr(sandbox.platform, 'machine', lambda: 'aarch64')
        for name, value in zip(('getuid', 'geteuid', 'getgid', 'getegid'), identity):
            patch.setattr(sandbox.os, name, lambda value=value: value)
        with pytest.raises(RuntimeError, match='unprivileged identity'):
            sandbox._check_identity()


def test_bootstrap_file_handles_are_closed_before_isolation(monkeypatch):
    closed = []

    def close(descriptor):
        closed.append(descriptor)
        if descriptor == 5:
            raise OSError(errno.EBADF, 'listdir descriptor already closed')

    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'listdir', lambda path: ['0', '1', '2', '3', '4', '5'])
        patch.setattr(sandbox.os, 'close', close)
        sandbox._close_extra_fds()
    assert closed == [3, 4, 5]


def test_installation_order_and_self_test_are_not_optional(monkeypatch):
    import docx  # Preload trusted modules before replacing listdir in the probe.
    import pypdf
    calls = []
    kernel = SimpleNamespace(restrict_privileges=lambda: calls.append('privileges'),
        restrict_files=lambda paths: calls.append(('files', paths)) or 3,
        restrict_syscalls=lambda: calls.append('syscalls'))
    monkeypatch.setattr(sandbox, '_check_identity', lambda: calls.append('identity'))
    monkeypatch.setattr(sandbox, '_close_extra_fds', lambda: calls.append('close_handles'))
    monkeypatch.setattr(sandbox, 'runtime_read_paths', lambda: ['runtime-only'])
    monkeypatch.setattr(sandbox, 'KernelPolicy', lambda: kernel)
    monkeypatch.setattr(sandbox, '_verify_denials', lambda: calls.append('verified'))
    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'listdir', lambda path: ['single-thread'])
        assert sandbox.enforce() == {'mode': sandbox.POLICY_VERSION, 'landlock_abi': 3}
    assert calls == ['identity', 'close_handles', 'privileges', ('files', ['runtime-only']), 'syscalls', 'verified']


@pytest.mark.parametrize('failure', ['files', 'syscalls', 'verification'])
def test_installation_failure_cannot_return_a_success_attestation(monkeypatch, failure):
    import docx
    import pypdf
    calls = []

    def stage(name):
        calls.append(name)
        if name == failure:
            raise RuntimeError('injected policy failure')
        return 3

    monkeypatch.setattr(sandbox, '_check_identity', lambda: None)
    monkeypatch.setattr(sandbox, '_close_extra_fds', lambda: None)
    monkeypatch.setattr(sandbox, 'runtime_read_paths', lambda: [])
    monkeypatch.setattr(sandbox, 'KernelPolicy', lambda: SimpleNamespace(
        restrict_privileges=lambda: stage('privileges'), restrict_files=lambda paths: stage('files'),
        restrict_syscalls=lambda: stage('syscalls')))
    monkeypatch.setattr(sandbox, '_verify_denials', lambda: stage('verification'))
    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'listdir', lambda path: ['single-thread'])
        with pytest.raises(RuntimeError, match='injected policy failure'):
            sandbox.enforce()
    assert calls[-1] == failure


@pytest.mark.parametrize('outcome', ['allowed', 'wrong_errno', 'denied'])
def test_live_policy_verification_requires_kernel_denial(monkeypatch, outcome):
    def open_file(*args):
        if outcome == 'allowed':
            return 33
        raise OSError(errno.EACCES if outcome == 'denied' else errno.ENOENT, 'synthetic probe')

    def no_socket(*args):
        raise OSError(errno.EPERM, 'synthetic probe')

    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'open', open_file)
        patch.setattr(sandbox.os, 'close', lambda fd: None)
        patch.setattr(sandbox.socket, 'socket', no_socket)
        if outcome == 'denied':
            sandbox._verify_denials()
        else:
            with pytest.raises(RuntimeError):
                sandbox._verify_denials()


@pytest.mark.parametrize('abi', [3, 4, 5, 6, 10])
def test_landlock_policy_is_read_only_and_closes_every_descriptor(monkeypatch, abi):
    policy = sandbox.KernelPolicy.__new__(sandbox.KernelPolicy)
    calls, closed = [], []

    def syscall(name, *args):
        if name == 'landlock_create_ruleset' and args[-1].value == 1:
            return abi
        if name == 'landlock_create_ruleset':
            calls.append(('handled', args[0]._obj.handled_access_fs))
            return 71
        if name == 'landlock_add_rule':
            rule = args[2]._obj
            calls.append(('allow', rule.allowed_access, rule.parent_fd))
        else:
            calls.append((name, args[0].value))
        return 0

    policy.syscall = syscall
    paths = [SimpleNamespace(is_dir=lambda: True), SimpleNamespace(is_dir=lambda: False)]
    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'O_PATH', 0x200000, raising=False)
        patch.setattr(sandbox.os, 'open', lambda path, flags: 72 if path is paths[0] else 73)
        patch.setattr(sandbox.os, 'close', closed.append)
        assert policy.restrict_files(paths) == abi
    assert calls == [('handled', (1 << (16 if abi >= 5 else 15)) - 1),
                     ('allow', sandbox.READ_FILE | sandbox.READ_DIR, 72),
                     ('allow', sandbox.READ_FILE, 73), ('landlock_restrict_self', 71)]
    assert closed == [72, 73, 71]


@pytest.mark.parametrize('abi', [1, 2])
def test_old_landlock_is_rejected_instead_of_weakening_policy(abi):
    policy = sandbox.KernelPolicy.__new__(sandbox.KernelPolicy)
    policy.syscall = lambda *args: abi
    with pytest.raises(RuntimeError, match='ABI 3'):
        policy.restrict_files([])


@pytest.mark.parametrize('failure', ['landlock_add_rule', 'landlock_restrict_self'])
def test_landlock_installation_failure_is_closed_and_descriptors_are_reaped(monkeypatch, failure):
    policy = sandbox.KernelPolicy.__new__(sandbox.KernelPolicy)
    closed = []

    def syscall(name, *args):
        if name == 'landlock_create_ruleset':
            return 3 if args[-1].value == 1 else 71
        if name == failure:
            raise RuntimeError('injected kernel failure')
        return 0

    policy.syscall = syscall
    with monkeypatch.context() as patch:
        patch.setattr(sandbox.os, 'O_PATH', 0x200000, raising=False)
        patch.setattr(sandbox.os, 'open', lambda *args: 72)
        patch.setattr(sandbox.os, 'close', closed.append)
        with pytest.raises(RuntimeError, match='injected kernel failure'):
            policy.restrict_files([SimpleNamespace(is_dir=lambda: True)])
    assert closed == [72, 71]


class SeccompFixture:
    def __init__(self, failure=None):
        self.failure = failure
        self.names = {name: number for number, name in enumerate((*sandbox.SYSCALLS, 'fcntl', 'fcntl64'), 1)}
        self.attributes, self.rules, self.released = [], [], []
        self.loaded = False

    def seccomp_init(self, default):
        assert default == sandbox.DENY
        return 99

    def seccomp_syscall_resolve_name(self, name):
        return self.names.get(name.decode(), -1)

    def seccomp_attr_set(self, context, attribute, value):
        assert context == 99
        self.attributes.append((attribute, value))
        return -1 if self.failure == 'attribute' else 0

    def seccomp_rule_add_array(self, context, action, number, count, arguments):
        assert context == 99
        comparison = None
        if arguments:
            item = arguments._obj
            comparison = (item.arg, item.op, item.datum_a, item.datum_b)
        self.rules.append((action, number, count, comparison))
        return -1 if self.failure == 'rule' else 0

    def seccomp_load(self, context):
        self.loaded = True
        return -1 if self.failure == 'load' else 0

    def seccomp_release(self, context):
        self.released.append(context)


def test_seccomp_is_native_arch_default_deny_with_only_read_only_fcntl():
    fixture = SeccompFixture()
    fixture.names['getdents'] = -1  # Missing architecture-specific call is not granted.
    policy = sandbox.KernelPolicy.__new__(sandbox.KernelPolicy)
    policy.seccomp = fixture
    policy.restrict_syscalls()
    assert fixture.attributes == [(2, sandbox.KILL_PROCESS), (4, 1)]
    assert fixture.loaded and fixture.released == [99]
    for action, number, count, comparison in fixture.rules:
        assert action == sandbox.ALLOW and number >= 0
        if count:
            assert number in (fixture.names['fcntl'], fixture.names['fcntl64'])
            assert comparison in ((1, 4, fcntl.F_GETFD, 0), (1, 4, fcntl.F_GETFL, 0))
        else:
            assert comparison is None


@pytest.mark.parametrize('failure', ['attribute', 'rule', 'load'])
def test_seccomp_failure_never_degrades_to_unfiltered_execution(failure):
    fixture = SeccompFixture(failure)
    policy = sandbox.KernelPolicy.__new__(sandbox.KernelPolicy)
    policy.seccomp = fixture
    with pytest.raises(RuntimeError):
        policy.restrict_syscalls()
    assert fixture.released == [99]


def test_read_policy_never_grants_workspace_home_or_secret_roots():
    paths = sandbox.runtime_read_paths()
    project = Path(__file__).resolve().parents[1]
    protected = [project / '.env', project.parent / '.env', project / 'data' / 'deepagent.db',
                 Path('/run/secrets/key'), Path('/etc/passwd'), Path('/proc/self/environ')]
    assert paths
    assert not any(file == root or root in file.parents for root in paths for file in protected)


def test_unsafe_runtime_layout_is_rejected(monkeypatch):
    monkeypatch.setattr(sandbox.sysconfig, 'get_path', lambda key: '/')
    with pytest.raises(RuntimeError, match='runtime library layout'):
        sandbox.runtime_read_paths()


@pytest.mark.parametrize('setting', ['false', 'off', 'yes', '1', 'garbage'])
def test_production_cannot_disable_or_misspell_os_sandbox(monkeypatch, setting):
    monkeypatch.setenv('DEEPAGENT_ENVIRONMENT', ' production ')
    monkeypatch.setenv('DEEPAGENT_PARSER_OS_SANDBOX', setting)
    with pytest.raises(ParseUnavailable, match='cannot be disabled'):
        IsolatedDocumentParser()


def test_production_and_explicit_development_opt_in_require_both_boundaries(monkeypatch):
    monkeypatch.setenv('DEEPAGENT_ENVIRONMENT', ' production ')
    monkeypatch.delenv('DEEPAGENT_PARSER_OS_SANDBOX', raising=False)
    assert IsolatedDocumentParser().require_os_sandbox
    monkeypatch.setenv('DEEPAGENT_ENVIRONMENT', 'development')
    monkeypatch.setenv('DEEPAGENT_PARSER_OS_SANDBOX', 'true')
    parser = IsolatedDocumentParser()
    assert parser.require_os_sandbox and parser.require_hard_memory


@pytest.mark.parametrize('proof', [None, {'mode': 'resource-only'}, {'mode': sandbox.POLICY_VERSION, 'landlock_abi': 2},
                                  {'mode': sandbox.POLICY_VERSION, 'landlock_abi': True}])
def test_controller_requires_os_policy_evidence_not_just_memory_limits(monkeypatch, proof):
    monkeypatch.setenv('DEEPAGENT_PARSER_OS_SANDBOX', 'true')
    parser = IsolatedDocumentParser()
    result = {'v': 1, 'status': 'ok', 'parser_version': parser.parser_version, 'chunker_version': parser.chunker_version,
        'memory_mode': 'address-space', 'sandbox': proof,
        'chunks': [{'position': 0, 'text': 'x', 'token_count': 1,
                    'content_hash': hashlib.sha256(b'x').hexdigest(), 'locator': {}}]}
    with pytest.raises(ParseProtocolError):
        parser._decode(json.dumps(result).encode(), 0)
    result['sandbox'] = {'mode': sandbox.POLICY_VERSION, 'landlock_abi': 3}
    assert parser._decode(json.dumps(result).encode(), 0)[0].text == 'x'


def test_worker_does_not_advertise_or_consume_when_parser_self_test_fails():
    service = KnowledgeService.__new__(KnowledgeService)
    started = []

    async def unavailable():
        raise ParseUnavailable('kernel policy unavailable')

    async def start():
        started.append(True)

    service.parser = SimpleNamespace(validate_runtime=unavailable)
    service.worker_lease = SimpleNamespace(start=start)
    with pytest.raises(ParseUnavailable):
        asyncio.run(service.start())
    assert not started


def test_failed_preflight_clears_previous_verified_state(monkeypatch):
    monkeypatch.setenv('DEEPAGENT_PARSER_OS_SANDBOX', 'true')
    parser = IsolatedDocumentParser()
    parser.runtime_verified = True

    async def unavailable(*args):
        raise ParseUnavailable('kernel policy unavailable')

    monkeypatch.setattr(parser, 'parse', unavailable)
    with pytest.raises(ParseUnavailable):
        asyncio.run(parser.validate_runtime())
    assert not parser.runtime_verified


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Native Landlock/seccomp requires Linux; mock tests are not acceptance')
def test_real_linux_native_syscalls_cannot_bypass_parser_policy(tmp_path):
    # On Linux missing Landlock/libseccomp or a root identity FAILS this gate.
    # A configured production target must not silently skip unavailable safety.
    secret = tmp_path / 'synthetic-secret.txt'
    secret.write_text('synthetic-test-secret-not-a-business-credential')
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).with_name('parser_os_fixture_worker.py')), str(secret)],
        capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    evidence = json.loads(result.stdout)
    assert evidence['policy']['mode'] == sandbox.POLICY_VERSION
    assert evidence['policy']['landlock_abi'] >= 3
    for name, call in evidence['calls'].items():
        assert call['result'] == -1, name
        assert call['errno'] in ({errno.EACCES, errno.EPERM} if name.startswith('outside_') else {errno.EPERM}), name
    assert not secret.with_name(secret.name + '.write').exists()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Native Landlock/seccomp requires Linux; mock tests are not acceptance')
def test_real_linux_isolated_formats_and_startup_self_test(monkeypatch):
    from test_parser_isolation import pdf
    from docx import Document
    import io
    monkeypatch.setenv('DEEPAGENT_PARSER_OS_SANDBOX', 'true')
    document = Document()
    document.add_paragraph('Linux isolation document.')
    output = io.BytesIO()
    document.save(output)
    fixtures = [(b'Linux isolation document.', 'text/plain', 'a.txt'),
        (b'# Heading\n\nLinux isolation document.', 'text/markdown', 'a.md'),
        (b'{"result": "Linux isolation document."}', 'application/json', 'a.json'),
        (b'one,two\nthree,four', 'text/csv', 'a.csv'),
        (b'<p>Linux isolation document.</p>', 'text/html', 'a.html'),
        (pdf(), 'application/pdf', 'a.pdf'),
        (output.getvalue(), 'application/octet-stream', 'a.docx')]
    parser = IsolatedDocumentParser()

    async def scenario():
        await parser.validate_runtime()
        assert parser.runtime_verified
        for content, mime, name in fixtures:
            assert await parser.parse(content, mime, name)
    asyncio.run(scenario())
