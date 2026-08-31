"""Fixed, read-only workspace inspection after execution has been drained.

Only Git's temporary index/objects are writable. No request-supplied command,
verification script, shell profile, global Git config, or agent tool is invoked.
This code runs in the sandbox service; it does not execute source on the Worker.
"""
from __future__ import annotations

import hashlib
import io
import re
import secrets
import shlex
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from packages.coding.changeset import ChangeSetBuilder
from packages.coding.errors import SandboxUnavailableError
from packages.sandbox.ports import SandboxSnapshot
from packages.sandbox.recovery_archive import normalize_recovery_archive


@dataclass(frozen=True)
class CancellationCapture:
    snapshot: SandboxSnapshot
    changes: dict


def safe_path(value):
    path = PurePosixPath(value)
    if (not value or not path.parts or path.is_absolute() or '..' in path.parts or '.git' in path.parts
            or '\\' in value or '\0' in value or str(path) != value):
        raise SandboxUnavailableError('Cancellation evidence contains an unsafe path')
    return value


class _InspectionBackend:
    def __init__(self, backend):
        self.backend = backend

    def execute(self, command, *, timeout=None):
        # Agent processes can alter /tmp/home/.gitconfig. It must not control
        # diff drivers, fsmonitor, hooks or the object directory during capture.
        prefix = ('env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent LANG=C.UTF-8 '
                  'GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null '
                  'GIT_CONFIG_COUNT=4 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/workspace/repo '
                  'GIT_CONFIG_KEY_1=core.fsmonitor GIT_CONFIG_VALUE_1=false '
                  'GIT_CONFIG_KEY_2=core.hooksPath GIT_CONFIG_VALUE_2=/dev/null '
                  'GIT_CONFIG_KEY_3=protocol.allow GIT_CONFIG_VALUE_3=never /bin/sh -c ')
        result = self.backend.execute(prefix + shlex.quote(command), timeout=30)
        if result.exit_code != 0 or result.truncated:
            raise SandboxUnavailableError('Cancellation Git inspection failed or exceeded its output limit')
        return result


def capture_changes(backend):
    inspection = _InspectionBackend(backend)
    with ChangeSetBuilder._temporary_index({'id': 'cancel_' + secrets.token_hex(16)}, inspection) as env:
        patch = inspection.execute(f'{env} git diff --binary --no-ext-diff --no-textconv --no-color HEAD').output
        numstat = inspection.execute(f'{env} git diff --numstat -z --no-ext-diff --no-textconv HEAD').output
        status = inspection.execute('git -c status.renames=false status --porcelain=v1 -z --untracked-files=all').output
    changed = ChangeSetBuilder._parse_status(status)
    for entry in changed:
        path = safe_path(entry['path'])
        if 'D' in entry['status']:
            entry['sha256'] = None
            continue
        responses = backend.download_files(['/workspace/repo/' + path])
        if len(responses) != 1 or responses[0].error or responses[0].content is None:
            raise SandboxUnavailableError('Cancellation evidence file could not be read')
        entry['sha256'] = hashlib.sha256(responses[0].content).hexdigest()
    return {'patch': patch, 'diff_stat': ChangeSetBuilder._parse_numstat(numstat), 'changed_files': changed}


def validate_capture(capture):
    snapshot, changes = capture.snapshot, capture.changes
    if (len(snapshot.content) != snapshot.size_bytes
            or hashlib.sha256(snapshot.content).hexdigest() != snapshot.sha256):
        raise SandboxUnavailableError('Cancellation snapshot integrity check failed')
    normalize_recovery_archive(snapshot.content)
    if (not isinstance(changes, dict) or set(changes) != {'patch', 'diff_stat', 'changed_files'}
            or not isinstance(changes['patch'], str) or len(changes['patch'].encode()) > 10_000_000
            or not isinstance(changes['diff_stat'], dict)
            or set(changes['diff_stat']) != {'files', 'added', 'deleted'}
            or any(type(value) is not int or value < 0 for value in changes['diff_stat'].values())
            or not isinstance(changes['changed_files'], list) or len(changes['changed_files']) > 100_000):
        raise SandboxUnavailableError('Cancellation change evidence is invalid')
    with tarfile.open(fileobj=io.BytesIO(snapshot.content), mode='r:') as archive:
        files = {member.name: member for member in archive if member.isfile()}
        seen = set()
        for entry in changes['changed_files']:
            if not isinstance(entry, dict) or set(entry) != {'path', 'status', 'sha256'}:
                raise SandboxUnavailableError('Cancellation file evidence is invalid')
            path = safe_path(entry['path'])
            if path in seen or not isinstance(entry['status'], str) or not re.fullmatch(r'[ MADRCU?!]{1,2}', entry['status']):
                raise SandboxUnavailableError('Cancellation file status is invalid')
            seen.add(path)
            member = files.get('workspace/repo/' + path)
            expected = hashlib.sha256(archive.extractfile(member).read()).hexdigest() if member else None
            if entry['sha256'] != expected or (expected is None and 'D' not in entry['status']):
                raise SandboxUnavailableError('Cancellation diff does not match its frozen snapshot')
