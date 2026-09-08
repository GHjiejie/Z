import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from apps import production_entrypoint as entry
from packages.operations.integration_checks import DOCKER_TESTS, IntegrationGate
from packages.operations.release_checks import NoSkippedAcceptance
from scripts import deployment_checks as deployment
from scripts import release
from packages.coding.changeset import ChangeSetBuilder
from packages.sandbox.docker_provider import DockerSandboxProvider
from packages.coding.errors import SandboxUnavailableError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_wheelhouse', ROOT / 'docker/platform/wheelhouse.py')
wheels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wheels)


@pytest.fixture
def wheelhouse(tmp_path):
    (tmp_path / 'example-1.0-py3-none-any.whl').write_bytes(b'synthetic-wheel')
    wheels.record(tmp_path)
    wheels.verify(tmp_path)
    return tmp_path


@pytest.mark.parametrize('mutation', ['tamper', 'missing', 'extra', 'symlink', 'manifest-symlink'])
def test_wheelhouse_detects_every_content_change(wheelhouse, mutation, tmp_path):
    target = wheelhouse / 'example-1.0-py3-none-any.whl'
    if mutation == 'tamper':
        target.write_bytes(b'changed')
    elif mutation == 'missing':
        target.unlink()
    elif mutation == 'extra':
        (wheelhouse / 'extra-1.0-py3-none-any.whl').write_bytes(b'extra')
    elif mutation == 'symlink':
        target.unlink()
        target.symlink_to(wheelhouse / wheels.MANIFEST)
    else:
        manifest = wheelhouse / wheels.MANIFEST
        manifest.unlink()
        manifest.symlink_to(target)
    with pytest.raises(ValueError):
        wheels.verify(wheelhouse)


def test_wheelhouse_rejects_empty_invalid_and_replaced_inventory(tmp_path, wheelhouse):
    empty = tmp_path / 'empty'
    empty.mkdir()
    with pytest.raises(ValueError, match='empty'):
        wheels.record(empty)
    assert not (empty / wheels.MANIFEST).exists()
    (empty / 'unexpected.txt').write_text('fixture')
    with pytest.raises(ValueError, match='Unexpected'):
        wheels.record(empty)
    with pytest.raises(ValueError, match='replace'):
        wheels.record(wheelhouse)


@pytest.mark.parametrize('role', entry.ROLES)
def test_release_command_is_a_fixed_module_not_a_shell(role):
    command = entry.command(role)
    assert command[:2] == [entry.sys.executable, '-m']
    assert command[2] in {'uvicorn', 'apps.platform_worker.main', 'packages.persistence.migrate', 'apps.sandbox_service.main'}
    if role == 'api':
        assert '--no-proxy-headers' in command and '--limit-concurrency' in command


@pytest.mark.parametrize('fault', ['none', 'root', 'macos', 'writable-root', 'writable-code', 'capabilities', 'privileges', 'docker', 'development'])
def test_release_entrypoint_enforces_runtime_boundaries(monkeypatch, fault):
    with monkeypatch.context() as patch:
        patch.setenv('DEEPAGENT_ENVIRONMENT', 'development' if fault == 'development' else 'production')
        patch.delenv('DOCKER_HOST', raising=False)
        patch.delenv('CONTAINER_HOST', raising=False)
        patch.setattr(entry.sys, 'platform', 'darwin' if fault == 'macos' else 'linux')
        patch.setattr(entry.os, 'geteuid', lambda: 0 if fault == 'root' else 10001)
        patch.setattr(entry.os, 'statvfs', lambda _: SimpleNamespace(f_flag=0 if fault == 'writable-root' else os.ST_RDONLY))
        patch.setattr(entry.os, 'access', lambda *args: fault == 'writable-code')
        patch.setattr(entry.Path, 'exists', lambda self: False)
        patch.setattr(entry.Path, 'read_text', lambda self: 'CapEff: ' + ('01' if fault == 'capabilities' else '00') +
            '\nNoNewPrivs: ' + ('0' if fault == 'privileges' else '1'))
        if fault == 'docker':
            patch.setenv('DOCKER_HOST', 'unix:///fixture.sock')
        if fault == 'none':
            entry.verify_release_runtime('worker')
        else:
            with pytest.raises(RuntimeError):
                entry.verify_release_runtime('worker')


@pytest.mark.parametrize('group', ['native', 'platform', 'docker'])
def test_critical_acceptance_skips_cannot_be_green(group):
    gate = NoSkippedAcceptance() if group == 'native' else IntegrationGate(group)
    gate.pytest_runtest_logreport(SimpleNamespace(skipped=True, nodeid='fixture[postgresql]',
        longrepr=('test.py', 1, 'Skipped: DEEPAGENT_TEST_POSTGRES_URL is required')))
    session = SimpleNamespace(exitstatus=0)
    gate.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1


def test_only_documented_sqlite_inapplicability_is_accepted():
    gate = IntegrationGate('platform')
    gate.pytest_runtest_logreport(SimpleNamespace(skipped=True, nodeid='fixture[sqlite]',
        longrepr=('test.py', 1, 'Skipped: SKIP LOCKED requires PostgreSQL')))
    assert not gate.skipped
    gate.pytest_runtest_logreport(SimpleNamespace(skipped=True, nodeid='fixture[postgresql]',
        longrepr=('test.py', 1, 'Skipped: SKIP LOCKED requires PostgreSQL')))
    assert gate.skipped
    docker = IntegrationGate('docker')
    docker.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(originalname=name) for name in DOCKER_TESTS[:-1]]))
    assert docker.skipped


@pytest.fixture(scope='module')
def resolved_configs(tmp_path_factory):
    if not shutil.which('docker'):
        pytest.skip('Compose CLI is required for real deployment-config validation')
    fixture = tmp_path_factory.mktemp('compose-config')
    config_file = fixture / 'nonsecret.env'
    config_file.write_text((ROOT / 'deploy/platform.env.example').read_text() + '\nDEEPAGENT_ENVIRONMENT=production\n')
    secret_file = fixture / 'synthetic-secret'
    secret_file.write_text('synthetic-only')
    secret_file.chmod(0o400)
    text = '\n'.join(file.read_text() for file in (ROOT / 'deploy').glob('*.compose.yaml'))
    environment = {key: value for key, value in os.environ.items() if key in {'PATH', 'SYSTEMROOT', 'DOCKER_CONFIG'}}
    for name in re.findall(r'\$\{([A-Z_]+):\?', text):
        environment[name] = str(secret_file)
    for name in ('API_IMAGE', 'WORKER_IMAGE', 'MIGRATION_IMAGE', 'SANDBOX_SERVICE_IMAGE'):
        environment[name] = 'registry.example.com/release@sha256:' + 'a' * 64
    environment.update(PLATFORM_ENV_FILE=str(config_file), SANDBOX_ENV_FILE=str(config_file),
                       DOCKER_SOCKET_GID='990', SANDBOX_BIND_ADDRESS='127.0.0.1')
    configs = {}
    for kind in deployment.SERVICES:
        # Platform must resolve without any migration image/secret variables.
        env = {key: value for key, value in environment.items() if kind != 'platform' or 'MIGRATION' not in key}
        result = subprocess.run(['docker', 'compose', '--env-file', os.devnull, '-p', 'deepagent-contract', '-f',
            str(ROOT / 'deploy' / (kind + '.compose.yaml')), 'config', '--format', 'json'],
            env=env, check=True, capture_output=True, text=True, timeout=30)
        configs[kind] = json.loads(result.stdout)
    return configs


@pytest.mark.parametrize('kind', deployment.SERVICES)
def test_real_compose_resolution_has_complete_safe_mounts(resolved_configs, kind):
    deployment.validate_config(resolved_configs[kind], kind)


@pytest.mark.parametrize('fault', ['tag', 'root', 'write', 'cap', 'inline-secret', 'docker-host', 'socket', 'extra-service', 'tmpfs', 'secret'])
def test_deployment_preflight_rejects_security_regressions(resolved_configs, fault):
    config = json.loads(json.dumps(resolved_configs['platform']))
    api = config['services']['api']
    if fault == 'tag':
        api['image'] = 'example:latest'
    elif fault == 'root':
        api['user'] = '0:0'
    elif fault == 'write':
        api['read_only'] = False
    elif fault == 'cap':
        api['cap_add'] = ['SYS_ADMIN']
    elif fault == 'inline-secret':
        api['environment']['OPENAI_API_KEY'] = 'synthetic-value'
    elif fault == 'docker-host':
        api['environment']['DOCKER_HOST'] = 'tcp://execution:2375'
    elif fault == 'socket':
        api['volumes'] = [{'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'}]
    elif fault == 'extra-service':
        config['services']['sandbox-service'] = api
    elif fault == 'tmpfs':
        api['tmpfs'] = ['/tmp:rw', 'noexec', 'size=64m']
    else:
        api['secrets'].append({'source': 'migration-database-url'})
    with pytest.raises(ValueError):
        deployment.validate_config(config, 'platform')


def test_deployment_checks_secret_metadata_without_reading_values(tmp_path, monkeypatch):
    secret = tmp_path / 'synthetic-secret'
    secret.write_text('not-read-by-preflight')
    secret.chmod(0o400)
    config = {'secrets': {'fixture': {'file': str(secret)}}}
    monkeypatch.setattr(Path, 'read_text', lambda *args: pytest.fail('must not read secret content'))
    with pytest.raises(ValueError, match='UID 10001'):
        deployment.check_secret_files(config)
    monkeypatch.setattr(Path, 'lstat', lambda _: SimpleNamespace(st_mode=stat_mode, st_uid=10001))
    stat_mode = 0o100400
    deployment.check_secret_files(config)
    stat_mode = 0o100444
    with pytest.raises(ValueError):
        deployment.check_secret_files(config)


def test_images_and_actions_are_pinned_and_secrets_excluded():
    dockerfile = (ROOT / 'docker/platform/Dockerfile').read_text()
    sources = re.findall(r'^FROM (\S+)', dockerfile, re.M)
    for source in sources:
        assert source in {'python-base', 'app-runtime'} or re.fullmatch(r'.+@sha256:[a-f0-9]{64}', source)
    assert 'COPY . .' not in dockerfile
    assert '--no-build-isolation --require-hashes' in dockerfile
    assert 'USER 10001:10001' in dockerfile
    assert 'git openssh-client libseccomp2' in dockerfile
    assert 'COPY --chown=0:0 apps/logging.json apps/logging.json' in dockerfile
    ignore = (ROOT / '.dockerignore').read_text()
    assert '!apps/logging.json' in ignore.splitlines()
    for pattern in ('**/.env', '**/.env.*', '**/*.key', '**/*.pem', '**/*.db', '**/.git/', '**/*.dagbackup', '**/.recovery/'):
        assert pattern in ignore.splitlines()
    workflow = (ROOT.parent / '.github/workflows/deepagent-release.yml')
    # This test belongs to the host integration group, not the parser-only image.
    content = workflow.read_text()
    actions = re.findall(r'uses: (\S+)', content)
    assert actions and all(re.fullmatch(r'actions/[^@]+@[a-f0-9]{40}', action) for action in actions)
    assert 'pull_request_target' not in content and 'persist-credentials: false' in content
    assert 'continue-on-error' not in content


@pytest.mark.parametrize('kind', ['https', 'ssh', 'both'])
def test_repository_trust_overlay_resolves_to_only_approved_public_files(resolved_configs, tmp_path, kind):
    base = tmp_path / 'resolved-base.json'
    base.write_text(json.dumps(resolved_configs['platform']))
    trust = tmp_path / 'public-trust'
    trust.write_text('public-trust-fixture-only')
    trust.chmod(0o444)
    env = {'PATH': os.environ['PATH'], 'REPOSITORY_CA_SOURCE_FILE': str(trust), 'REPOSITORY_KNOWN_HOSTS_SOURCE_FILE': str(trust)}
    files = ['-f', str(base)]
    for transport in ('https', 'ssh'):
        if kind in {transport, 'both'}:
            files += ['-f', str(ROOT / 'deploy' / ('repository-' + transport + '.compose.yaml'))]
    result = subprocess.run(['docker', 'compose', '--env-file', os.devnull, '-p', 'deepagent-contract',
        *files, 'config', '--format', 'json'], env=env, check=True, capture_output=True, text=True, timeout=30)
    config = json.loads(result.stdout)
    deployment.validate_config(config, 'platform')
    assert len(config['services']['api']['configs']) == (2 if kind == 'both' else 1)
    deployment.check_secret_files({'configs': config['configs']})
    for fault in ('target', 'writable', 'inline', 'unexpected', 'missing', 'wrong-env'):
        changed = json.loads(json.dumps(config))
        item = changed['services']['api']['configs'][0]
        if fault == 'target':
            item['target'] = '/app/packages/repositories/network.py'
        elif fault == 'writable':
            item['mode'] = 0o666
        elif fault == 'inline':
            changed['configs'][item['source']] = {'content': 'inline'}
        elif fault == 'unexpected':
            changed['services']['worker']['configs'] = [item]
        elif fault == 'missing':
            changed['services']['api']['configs'] = []
        else:
            variable, _ = deployment.REPOSITORY_TRUST[item['source']]
            changed['services']['api']['environment'][variable] = '/tmp/unapproved'
        with pytest.raises(ValueError):
            deployment.validate_config(changed, 'platform')


@pytest.mark.parametrize('fault', ['symlink', 'writable', 'empty'])
def test_public_repository_trust_file_metadata_fails_closed(tmp_path, fault):
    trust = tmp_path / 'trust'
    trust.write_text('public fixture')
    trust.chmod(0o444)
    if fault == 'symlink':
        source = tmp_path / 'link'
        source.symlink_to(trust)
    else:
        source = trust
        trust.chmod(0o644)
        if fault == 'writable':
            trust.chmod(0o666)
        else:
            trust.write_text('')
    with pytest.raises(ValueError, match='Public trust'):
        deployment.check_secret_files({'configs': {'repository-ca': {'file': str(source)}}})


def test_release_scan_refuses_uncommitted_source(monkeypatch, tmp_path):
    monkeypatch.setattr(release, 'require_docker', lambda: None)
    monkeypatch.setattr(release, 'run', lambda *args, **kwargs: pytest.fail('no scanning before revision validation'))
    with pytest.raises(ValueError, match='commit SHA'):
        release.scan_images('uncommitted', tmp_path / 'reports')


@pytest.mark.parametrize('fails', [False, True])
def test_changeset_temporary_metadata_is_cleaned_before_snapshot(fails):
    commands = []
    backend = SimpleNamespace(execute_platform=lambda cmd: commands.append(cmd) or SimpleNamespace(exit_code=0))
    backend.execute = backend.execute_platform
    with pytest.raises(ValueError) if fails else __import__('contextlib').nullcontext():
        with ChangeSetBuilder._temporary_index({'id': 'run_fixture'}, backend) as environment:
            assert 'GIT_INDEX_FILE=' in environment
            if fails:
                raise ValueError('synthetic diff failure')
    assert len(commands) == 2
    assert 'ln -s /workspace/repo/.git/objects' in commands[0]
    assert commands[1] == ('rm -f -- /tmp/deepagent-changeset-run_fixture.index '
        '/tmp/deepagent-base-objects-run_fixture && rm -rf -- /tmp/deepagent-objects-run_fixture')


def test_changeset_cleanup_failure_cannot_produce_recovery_evidence():
    calls = []

    def execute(cmd):
        calls.append(cmd)
        return SimpleNamespace(exit_code=0 if len(calls) == 1 else 1)

    backend = SimpleNamespace(execute=execute, execute_platform=execute)
    with pytest.raises(RuntimeError, match='remove temporary'):
        with ChangeSetBuilder._temporary_index({'id': 'run_fixture'}, backend):
            pass


def test_legacy_tmpfs_cannot_silently_produce_empty_recovery():
    container = SimpleNamespace(attrs={'HostConfig': {'Tmpfs': {'/artifacts': 'rw'}}})
    with pytest.raises(SandboxUnavailableError, match='Legacy volatile'):
        DockerSandboxProvider._require_archivable_mounts(container)


@pytest.mark.asyncio
async def test_revoked_execution_does_not_attempt_partial_writes():
    from packages.runtime.deepagents_executor import DeepAgentsRuntimeExecutor
    from packages.persistence.fencing import LeaseLostError

    def lost():
        raise LeaseLostError('replaced')

    executor = object.__new__(DeepAgentsRuntimeExecutor)
    executor.db = SimpleNamespace(assert_execution_fence=lost)
    await executor._preserve_partial({'id': 'run_fixture'}, {}, None, 'run_cancelled')
