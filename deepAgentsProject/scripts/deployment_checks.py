"""Read-only checks of the resolved deployment, never start or alter services."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SERVICES = {'platform': {'api', 'worker'}, 'migration': {'migrate'}, 'sandbox': {'sandbox-service'}}
PLATFORM_SECRETS = {'database-url', 'bootstrap-password', 'model-api-key', 'embedding-api-key',
                    'sandbox-token', 'sandbox-ca', 'controller-cert', 'controller-key',
                    'metrics-token', 'otlp-token', 'telemetry-ca'}
SECRETS = {'api': PLATFORM_SECRETS, 'worker': PLATFORM_SECRETS | {'worker-metrics-cert','worker-metrics-key'}, 'migrate': {'migration-database-url'},
    'sandbox-service': {'sandbox-token', 'lease-database-url', 'sandbox-cert', 'sandbox-key', 'controller-ca'}}
REPOSITORY_TRUST = {
    'repository-ca': ('DEEPAGENT_REPOSITORY_CA_FILE', '/run/repository-trust/ca.pem'),
    'repository-known-hosts': ('DEEPAGENT_REPOSITORY_SSH_KNOWN_HOSTS_FILE', '/run/repository-trust/known_hosts'),
}


def validate_config(config: dict, kind: str) -> None:
    if kind not in SERVICES or set(config.get('services', {})) != SERVICES[kind]:
        raise ValueError('Deployment must contain exactly the services for its host role')
    for role, service in config['services'].items():
        def require(condition, reason):
            if not condition:
                raise ValueError(f'{role}: {reason}')

        require(re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}', service.get('image', '')),
                'an immutable registry image digest is required')
        require(service.get('command') == [role] and not service.get('entrypoint'), 'release entrypoint cannot be overridden')
        require(service.get('user') == '10001:10001' and service.get('read_only') is True, 'non-root read-only runtime required')
        require(service.get('cap_drop') == ['ALL'] and not service.get('cap_add') and not service.get('privileged'), 'capabilities must be dropped')
        require(any(option.replace('=', ':') == 'no-new-privileges:true' for option in service.get('security_opt', [])), 'no-new-privileges required')
        require(not any(option.startswith(('seccomp:unconfined', 'seccomp=unconfined')) for option in service.get('security_opt', [])), 'unconfined seccomp is forbidden')
        require(not any(service.get(key) for key in ('devices', 'device_cgroup_rules', 'volumes_from', 'use_api_socket')), 'extra host device/socket access is forbidden')
        require(all(service.get(key) != 'host' for key in ('network_mode', 'pid', 'ipc', 'userns_mode')), 'host namespaces are forbidden')
        require(all(math.isfinite(float(service.get(key, 0))) and float(service.get(key, 0)) > 0
                    for key in ('cpus', 'mem_limit', 'pids_limit')), 'finite CPU, memory and PID limits required')
        environment = service.get('environment', {})
        require(environment.get('DEEPAGENT_ENVIRONMENT') == 'production', 'production environment required')
        assigned_configs = service.get('configs', [])
        expected_configs = {name for name, (variable, _) in REPOSITORY_TRUST.items() if environment.get(variable)}
        require(not expected_configs or role == 'api', 'repository trust belongs only to the repository API')
        require(len(assigned_configs) == len(expected_configs)
            and {item.get('source') for item in assigned_configs} == expected_configs, 'unexpected or missing public trust configs')
        for item in assigned_configs:
            variable, target = REPOSITORY_TRUST[item['source']]
            require(item.get('target') == target and environment.get(variable) == target
                and item.get('mode') in (0o444, '0444') and not item.get('uid') and not item.get('gid'), 'invalid repository public trust mount')
        if role in {'api', 'worker'}:
            for key, name in (('DEEPAGENT_METRICS_TOKEN_FILE','metrics-token'),
                              ('DEEPAGENT_OTLP_TOKEN_FILE','otlp-token'), ('DEEPAGENT_OTLP_CA_FILE','telemetry-ca')):
                require(environment.get(key) == '/run/secrets/' + name, 'telemetry credentials and CA must use assigned secret files')
            collector = urlsplit(environment.get('DEEPAGENT_OTLP_TRACES_ENDPOINT',''))
            require(collector.scheme == 'https' and collector.hostname and collector.path == '/v1/traces'
                and not (collector.username or collector.password or collector.query or collector.fragment),
                'an explicit credential-free HTTPS OTLP endpoint is required')
            require(environment.get('OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED') == 'true', 'trace queue-loss observations required')
        if role == 'worker':
            require(str(environment.get('DEEPAGENT_WORKER_METRICS_PORT','')).isdigit()
                and 1 <= int(environment['DEEPAGENT_WORKER_METRICS_PORT']) <= 65535, 'Worker metrics listener required')
            require(environment.get('DEEPAGENT_METRICS_TLS_CERT_FILE') == '/run/secrets/worker-metrics-cert'
                and environment.get('DEEPAGENT_METRICS_TLS_KEY_FILE') == '/run/secrets/worker-metrics-key',
                'Worker management listener requires its own TLS files')
        for key, value in environment.items():
            require(not (value and (key in {'DATABASE_URL', 'SANDBOX_LEASE_DATABASE_URL', 'DOCKER_HOST', 'CONTAINER_HOST'}
                or re.search(r'(?:PASSWORD|TOKEN|SECRET|API_KEY)$', key))), 'inline credentials and daemon endpoints are forbidden')
        assigned = service.get('secrets', [])
        require({item['source'] for item in assigned} == SECRETS[role], 'unexpected or missing secret access')
        require(all(item.get('target', item['source']) in {item['source'], '/run/secrets/' + item['source']}
                    for item in assigned), 'secret targets cannot be remapped')
        require(len(assigned) == len(SECRETS[role]), 'duplicate secret assignment')
        mounts = service.get('tmpfs', [])
        expected = {'/tmp', '/var/lib/deepagent'} if role in {'api', 'worker'} else {'/tmp'}
        require(len(mounts) == len(expected), 'unexpected temporary mounts')
        targets = set()
        for mount in mounts:
            target, separator, options = mount.partition(':')
            opts = options.split(',')
            require(separator and target in expected, 'temporary mount must be one complete absolute specification')
            require({'rw', 'noexec', 'nosuid', 'nodev', 'uid=10001', 'gid=10001', 'mode=0700'}.issubset(opts), 'unsafe temporary mount permissions')
            sizes = [option for option in opts if option.startswith('size=')]
            require(len(sizes) == 1 and re.fullmatch(r'size=[1-9][0-9]*[mk]', sizes[0]), 'bounded temporary mount required')
            require(len(opts) == 8, 'unexpected temporary mount options')
            targets.add(target)
        require(targets == expected, 'missing temporary mount')
        volumes = service.get('volumes', [])
        if role == 'sandbox-service':
            require(len(volumes) == 2, 'only Docker socket and dedicated state volume are allowed')
            by_target = {volume['target']: volume for volume in volumes}
            socket = by_target.get('/var/run/docker.sock', {})
            state = by_target.get('/var/lib/deepagent', {})
            require(socket.get('type') == 'bind' and socket.get('source') == '/var/run/docker.sock'
                and socket.get('read_only') is True and state.get('type') == 'volume', 'invalid execution host mounts')
            require(len(service.get('group_add', [])) == 1 and str(service['group_add'][0]).isdigit()
                and int(service['group_add'][0]) > 0, 'dedicated Docker socket group required')
        else:
            require(not volumes and not service.get('group_add'), 'platform roles must not mount host paths or Docker sockets')
        if role in {'worker', 'migrate'}:
            require(not service.get('ports'), 'this role must not expose a public port')
    used_configs = {item['source'] for service in config['services'].values() for item in service.get('configs', [])}
    if set(config.get('configs', {})) != used_configs:
        raise ValueError('Unexpected public trust source definitions')
    for source in config.get('configs', {}).values():
        if not set(source).issubset({'file', 'name'}) or not Path(source.get('file', '')).is_absolute():
            raise ValueError('Public trust must use explicit file sources, never inline content or external configs')


def check_secret_files(config: dict) -> None:
    # Compose file-backed secrets retain host ownership/modes. Long-syntax uid/
    # mode alone cannot fix that. Inspect metadata only; never read secret values.
    for name, secret in config.get('secrets', {}).items():
        path = Path(secret.get('file', ''))
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001
                or info.st_mode & 0o077 or not info.st_mode & stat.S_IRUSR):
            raise ValueError(f'Secret {name} must be a regular owner-readable file owned by UID 10001 with no group/other access')
    for name, source in config.get('configs', {}).items():
        info = Path(source['file']).lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or not info.st_mode & stat.S_IROTH or not 0 < info.st_size <= 1024 * 1024):
            raise ValueError(f'Public trust {name} must be a bounded, non-symlink, readable file without group/other write access')


def validate_deployment(kind: str, *, repository_trust: str | None = None) -> None:
    if kind not in SERVICES:
        raise ValueError('Unknown deployment kind')
    if repository_trust is not None and (kind != 'platform' or repository_trust not in {'https', 'ssh', 'both'}):
        raise ValueError('Repository public trust overlays apply only to the platform deployment')
    files = ['--file', str(ROOT / 'deploy' / (kind + '.compose.yaml'))]
    for transport in ('https', 'ssh'):
        if repository_trust in {transport, 'both'}:
            files += ['--file', str(ROOT / 'deploy' / ('repository-' + transport + '.compose.yaml'))]
    # Explicit configuration; do not implicitly import this checkout's .env.
    result = subprocess.run(['docker', 'compose', '--env-file', os.devnull, '--project-name', 'deepagent-' + kind,
        *files, 'config', '--format', 'json'],
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Compose configuration resolution failed; verify required deployment variables privately')
    config = json.loads(result.stdout)
    validate_config(config, kind)
    check_secret_files(config)
    print('Deployment configuration and secret-file metadata passed; services were not started.')
