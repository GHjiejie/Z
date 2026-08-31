"""Build matching PostgreSQL clients from a hash-verified official source release.

No system install, service startup, credentials or database access.
"""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

VERSION = '17.11'
SHA256 = 'dd27f2b3c59e73ed14aa3324901242bf69a032a6347805f274e6260322d42979'
URL = f'https://ftp.postgresql.org/pub/source/v{VERSION}/postgresql-{VERSION}.tar.bz2'


def build(output):
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('PostgreSQL client output must be a new directory')
    output.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix='deepagent-pg-client-build-') as temporary:
        root = Path(temporary)
        archive = root / 'postgresql.tar.bz2'
        checksum, size = hashlib.sha256(), 0
        with urllib.request.urlopen(URL, timeout=30) as source, archive.open('xb') as target:
            if source.url != URL:
                raise ValueError('PostgreSQL source redirect is not approved')
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    raise ValueError('PostgreSQL source archive exceeds its size limit')
                checksum.update(chunk)
                target.write(chunk)
        if checksum.hexdigest() != SHA256:
            raise ValueError('PostgreSQL source checksum mismatch')
        with tarfile.open(archive, 'r:bz2') as source:
            source.extractall(root, filter='data')
        source = root / ('postgresql-' + VERSION)
        with (output / 'build.log').open('x') as log:
            for command in (
                ['./configure', '--prefix=' + str(output), '--with-openssl', '--without-readline', '--without-icu'],
                ['make', '-C', 'src/backend', 'generated-headers'],
                # Component-only recursive builds share generated archives;
                # serialize this path to avoid duplicate libpgcommon writers.
                ['make', '-C', 'src/bin/pg_dump', 'all'],
                ['make', '-C', 'src/interfaces/libpq', 'install'],
                ['make', '-C', 'src/bin/pg_dump', 'install'],
            ):
                subprocess.run(command, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
        for name in ('pg_dump', 'pg_restore'):
            result = subprocess.run([str(output / 'bin' / name), '--version'], check=True, capture_output=True, text=True, timeout=10)
            if f'(PostgreSQL) {VERSION}' not in result.stdout:
                raise ValueError('Built PostgreSQL client version did not match verified source')
    print('Verified PostgreSQL clients built; no server or system package was installed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)
