"""Authenticated, bounded recovery bundles. Never consume unauthenticated plaintext."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tarfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b'DEEPAGENT-RECOVERY-1\n'
CHUNK = 1024 * 1024
MAX_MANIFEST = 64 * CHUNK
# Below GCM's per-message bound; large installations need chunked/PITR backups.
MAX_BUNDLE = 32 * 1024 * CHUNK


class RecoveryError(RuntimeError):
    """Safe operator-facing failure, without credentials or database contents."""


def private_directory(path: Path) -> Path:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RecoveryError('Recovery directory must be an owned, private directory (0700)')
    return path.resolve(strict=True)


def read_private_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RecoveryError('Recovery credential must be an owned private regular file')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise RecoveryError('Recovery credential exceeds its size limit')
        return data


def private_write(path: Path, content: bytes):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def digest(path: Path):
    result, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while data := stream.read(CHUNK):
            result.update(data)
            size += len(data)
    return {'sha256': result.hexdigest(), 'size': size}


def seal(root: Path, manifest: dict, destination: Path, key: bytes):
    if len(key) != 32:
        raise RecoveryError('Recovery encryption requires a 32-byte binary key')
    parent = private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise RecoveryError('Refusing to overwrite an existing recovery bundle')
    private_write(root / 'manifest.json', json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    if (root / 'manifest.json').stat().st_size > MAX_MANIFEST:
        raise RecoveryError('Recovery inventory exceeds its limit')
    archive = root / 'bundle.tar'
    if sum(item['size'] + 2048 for item in manifest['files'].values()) + MAX_MANIFEST > MAX_BUNDLE:
        raise RecoveryError('Recovery bundle exceeds its limit')
    with tarfile.open(archive, 'x:') as bundle:
        for name in ['manifest.json', *sorted(manifest['files'])]:
            bundle.add(root / name, arcname=name, recursive=False)
    archive.chmod(0o600)
    if archive.stat().st_size > MAX_BUNDLE:
        raise RecoveryError('Recovery bundle exceeds its limit')
    # Temporary output is on the destination filesystem for atomic no-clobber publication.
    staging = parent / ('.recovery-' + secrets.token_hex(16))
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output, archive.open('rb') as source:
            nonce = secrets.token_bytes(12)
            header = MAGIC + nonce
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            output.write(header)
            while data := source.read(CHUNK):
                output.write(encryptor.update(data))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        os.link(staging, destination)  # fails if another publisher won; never replaces it
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        staging.unlink(missing_ok=True)  # only our uniquely created temporary output


def unseal(source: Path, root: Path, key: bytes) -> dict:
    if len(key) != 32:
        raise RecoveryError('Recovery encryption requires a 32-byte binary key')
    archive = root / 'verified.tar'
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not len(MAGIC) + 28 <= info.st_size <= MAX_BUNDLE + len(MAGIC) + 28:
                raise RecoveryError('Invalid recovery bundle size or file type')
            header = stream.read(len(MAGIC) + 12)
            if header[:len(MAGIC)] != MAGIC:
                raise RecoveryError('Unsupported recovery bundle format')
            stream.seek(-16, os.SEEK_END)
            tag = stream.read(16)
            stream.seek(len(header))
            decryptor = Cipher(algorithms.AES(key), modes.GCM(header[-12:], tag)).decryptor()
            decryptor.authenticate_additional_data(header)
            remaining = info.st_size - len(header) - 16
            private_write(archive, b'')
            with archive.open('wb') as output:
                while remaining:
                    data = stream.read(min(CHUNK, remaining))
                    if not data:
                        raise RecoveryError('Truncated recovery bundle')
                    output.write(decryptor.update(data))
                    remaining -= len(data)
                output.write(decryptor.finalize())
        # GCM authentication must finish before tar parsing or any database/object writes.
        with tarfile.open(archive, 'r:') as bundle:
            first = bundle.next()
            if first is None or first.name != 'manifest.json' or not first.isfile() or first.size > MAX_MANIFEST:
                raise RecoveryError('Missing or invalid recovery inventory')
            manifest = json.load(bundle.extractfile(first))
            files = manifest.get('files', {})
            if manifest.get('format') != 1 or not isinstance(files, dict) or 'database.dump' not in files or len(files) > 100000:
                raise RecoveryError('Unsupported recovery inventory')
            seen = set()
            while (member := bundle.next()) is not None:
                import re
                name = member.name
                if (name in seen or name not in files or not member.isfile()
                        or not (name == 'database.dump' or re.fullmatch(r'objects/[a-f0-9]{64}', name))
                        or member.size != files[name]['size']
                        or (name.startswith('objects/') and member.size > 100 * CHUNK)):
                    raise RecoveryError('Unexpected, duplicate or unsafe recovery archive member')
                seen.add(name)
                target = root / name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                private_write(target, b'')
                with target.open('wb') as output, bundle.extractfile(member) as content:
                    total = 0
                    while data := content.read(CHUNK):
                        total += len(data)
                        if total > member.size:
                            raise RecoveryError('Recovery member exceeds its declared size')
                        output.write(data)
                if digest(target) != files[name]:
                    raise RecoveryError('Recovery member content verification failed')
            if seen != set(files):
                raise RecoveryError('Incomplete recovery archive')
        return manifest
    except RecoveryError:
        raise
    except Exception:
        raise RecoveryError('Recovery authentication or inventory verification failed') from None
