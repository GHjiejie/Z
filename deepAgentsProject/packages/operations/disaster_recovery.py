"""Operator-owned PostgreSQL + immutable object backup and quarantined restore.

No deployment, overwrite, source mutation, role restoration or automatic promotion.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from packages.operations.recovery_bundle import (
    RecoveryError, MAX_BUNDLE, MAX_MANIFEST, digest, private_directory, private_write, read_private_file, seal, unseal,
)
from packages.persistence.database import LATEST_SCHEMA_VERSION
from packages.persistence.archive_store import SharedArchiveStore

QUARANTINE = 'deepagent:recovery:quarantined:'
TABLES = {'knowledge_document_versions', 'repository_snapshots', 'workspace_snapshots'}
MAX_OBJECT = 100 * 1024 * 1024


def assert_not_recovery_database(db):
    if db.dialect == 'postgresql':
        row = db.fetch_one("""SELECT shobj_description(oid,'pg_database') AS marker
            FROM pg_database WHERE datname=current_database()""")
        if ((row or {}).get('marker') or '').startswith(QUARANTINE):
            raise RecoveryError('Restored database is quarantined; production activation requires a reviewed recovery procedure')


def connection_settings(dsn: str, *, development=False):
    if any(name.startswith('PG') for name in os.environ):
        raise RecoveryError('Recovery refuses ambient PG* settings; use only the explicit private connection file')
    try:
        values = conninfo_to_dict(dsn)
    except Exception:
        raise RecoveryError('Invalid recovery database connection configuration') from None
    allowed = {'host', 'hostaddr', 'port', 'dbname', 'user', 'password', 'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'connect_timeout'}
    if set(values) - allowed or not all(values.get(key) for key in ('host', 'dbname', 'user')):
        raise RecoveryError('Recovery requires an explicit dedicated database, host and user; service/options overrides are prohibited')
    if any('\n' in value or '\r' in value or '\x00' in value for value in values.values()):
        raise RecoveryError('Invalid recovery connection value')
    if development:
        if values['host'] not in {'127.0.0.1', 'localhost', '::1'} or values.get('hostaddr', values['host']) not in {'127.0.0.1', 'localhost', '::1'}:
            raise RecoveryError('Development recovery is limited to loopback PostgreSQL')
    elif values.get('sslmode') != 'verify-full' or not values.get('sslrootcert'):
        raise RecoveryError('Recovery PostgreSQL connections require verify-full and an explicit CA')
    values['connect_timeout'] = '5'
    return values


@contextmanager
def connection(values, *, autocommit=False):
    # Explicit parameters, not caller-provided libpq service/options/proxy state.
    with tempfile.NamedTemporaryFile(prefix='deepagent-empty-pgpass-') as empty:
        with psycopg.connect(**values, passfile=empty.name, autocommit=autocommit, row_factory=dict_row,
                options='-c statement_timeout=1800000 -c lock_timeout=5000 -c timezone=UTC -c datestyle=ISO -c bytea_output=hex') as conn:
            yield conn


def server_policy(conn, *, development, version=None):
    if version is not None and conn.info.server_version // 10000 != version // 10000:
        raise RecoveryError('Restore requires the same PostgreSQL major version as the backup')
    if not development and conn.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()['rolsuper']:
        raise RecoveryError('Production recovery requires dedicated non-superuser credentials')


@contextmanager
def utility_environment(values, root):
    # Service contents stay in a 0600 file, never in process argv or stdout.
    path = root / ('pgservice-' + secrets.token_hex(8))
    empty = root / ('empty-pgpass-' + secrets.token_hex(8))
    private_write(empty, b'')
    settings = {**values, 'passfile': str(empty)}
    private_write(path, ('[recovery]\n' + ''.join(f'{key}={value}\n' for key, value in settings.items())).encode())
    try:
        yield {'PATH': os.defpath, 'LC_ALL': 'C', 'PGSERVICEFILE': str(path), 'PGSERVICE': 'recovery',
            'PGOPTIONS': '-c statement_timeout=1800000 -c lock_timeout=5000 -c default_transaction_read_only=off'}
    finally:
        path.unlink()
        empty.unlink()


def utility(binary, arguments, values, root):
    binary = Path(binary).resolve(strict=True)
    if binary.name not in {'pg_dump', 'pg_restore'} or not binary.is_file():
        raise RecoveryError('Use an explicit PostgreSQL pg_dump or pg_restore executable')
    with utility_environment(values, root) as environment:
        # Private bounded stderr file; native messages can contain source secrets.
        error_file = root / ('pg-error-' + secrets.token_hex(8))
        private_write(error_file, b'')
        with error_file.open('wb') as errors:
            try:
                # Set the native child's output-file ceiling without unsafe preexec_fn
                # hooks in a multithreaded parent. Only fixed Python code is executed.
                wrapper = ('import os,resource,sys;'
                    f'resource.setrlimit(resource.RLIMIT_FSIZE,({MAX_BUNDLE},{MAX_BUNDLE}));'
                    'os.execv(sys.argv[1],sys.argv[1:])')
                result = subprocess.run([sys.executable, '-c', wrapper, str(binary), *arguments], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=errors, env=environment, timeout=1800)
            except (OSError, subprocess.SubprocessError):
                raise RecoveryError('PostgreSQL recovery utility could not complete within its deadline') from None
        if result.returncode or error_file.stat().st_size:
            raise RecoveryError('PostgreSQL recovery utility failed or warned; no backup/restore acceptance was granted')


def fingerprints(conn):
    relations = conn.execute("""SELECT n.nspname AS schema,c.relname AS name FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')
        ORDER BY n.nspname,c.relname""").fetchall()
    result = []
    for relation in relations:
        if relation['schema'] != 'public':
            raise RecoveryError('Recovery currently requires a dedicated platform database in the public schema')
        checksum, count = hashlib.sha256(), 0
        query = sql.SQL('SELECT row_to_json(t)::text AS body FROM {} t ORDER BY (row_to_json(t)::text) COLLATE "C"').format(
            sql.Identifier(relation['schema'], relation['name']))
        with conn.cursor(name='inventory_' + secrets.token_hex(8)) as rows:
            rows.execute(query)
            for row in rows:
                data = row['body'].encode()
                checksum.update(len(data).to_bytes(8, 'big'))
                checksum.update(data)
                count += 1
        result.append({**relation, 'rows': count, 'sha256': checksum.hexdigest()})
    return result


def schema_version(conn):
    versions = conn.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall()
    if [row['version'] for row in versions] != list(range(1, LATEST_SCHEMA_VERSION + 1)):
        raise RecoveryError('Backup and recovery require the exact current application schema')
    from langgraph.checkpoint.postgres import PostgresSaver
    checkpoint = conn.execute('SELECT MAX(v) AS version FROM checkpoint_migrations').fetchone()['version']
    if checkpoint != len(PostgresSaver.MIGRATIONS) - 1:
        raise RecoveryError('Checkpoint schema is missing or incompatible')
    return LATEST_SCHEMA_VERSION


def object_inventory(conn, storage, root, *, materialize=True):
    objects, files = [], {}
    total_bytes = 0
    directory = root / 'objects'
    if materialize:
        directory.mkdir(mode=0o700)

    def record(table, row, content, key, version):
        nonlocal total_bytes
        if len(objects) >= 99999:
            raise RecoveryError('Recovery inventory exceeds its limit')
        if len(content) > MAX_OBJECT:
            raise RecoveryError('Backup object exceeds platform transfer limit')
        checksum = hashlib.sha256(content).hexdigest()
        expected = row.get('archive_sha256') or row.get('content_sha256')
        if expected and checksum != expected or row.get('size_bytes') is not None and row['size_bytes'] != len(content):
            raise RecoveryError('Referenced immutable object failed content verification')
        name = 'objects/' + checksum
        if name not in files:
            if total_bytes + len(content) + MAX_MANIFEST > MAX_BUNDLE:
                raise RecoveryError('Recovery object inventory exceeds its size limit')
            total_bytes += len(content)
            if materialize:
                private_write(root / name, content)
            files[name] = {'sha256': checksum, 'size': len(content)}
        objects.append({'table': table, 'id': row['id'], 'key': key, 'version': version,
            'bucket': storage.bucket, 'file': name, 'content_type': row.get('content_type', 'application/octet-stream')})

    with conn.cursor(name='knowledge_objects') as rows:
        rows.execute('SELECT * FROM knowledge_document_versions ORDER BY id')
        for row in rows:
            if row['bucket'] != storage.bucket or row['storage_provider'] != storage.provider:
                raise RecoveryError('Object storage configuration does not match the database inventory')
            version = row['object_version_id']
            if not version:
                if row['status'] not in {'PENDING_UPLOAD', 'FAILED', 'EXPIRED'} or row['content_sha256']:
                    raise RecoveryError('A committed document is missing immutable object provenance')
                continue  # Unfinished uploads have no committed bytes to recover.
            record('knowledge_document_versions', row, storage.get_content(row['object_key'], version), row['object_key'], version)
    archives = SharedArchiveStore(storage)
    for table, kind in (('repository_snapshots', 'repository'), ('workspace_snapshots', 'workspace'), ('repository_objects', 'repository')):
        with conn.cursor(name=table + '_objects') as rows:
            query = 'SELECT * FROM {}' + (" WHERE status='READY'" if table == 'repository_objects' else '') + ' ORDER BY id'
            rows.execute(sql.SQL(query).format(sql.Identifier(table)))
            for row in rows:
                content = archives.read(row, kind=kind)  # verifies fixed version, tenant scope, digest and size
                uri = urlsplit(row['archive_path'])
                record(table, row, content, uri.path.removeprefix('/'), parse_qs(uri.query)['version'][0])
    return objects, files


def backup(dsn, storage, destination, key, scratch, pg_dump, *, development=False):
    started = time.monotonic()
    values = connection_settings(dsn, development=development)
    private_directory(scratch)
    private_directory(destination.parent)
    if storage.provider == 'local':
        raise RecoveryError('Production recovery requires immutable versioned objects, not local files')
    with tempfile.TemporaryDirectory(prefix='deepagent-backup-', dir=scratch) as temporary:
        root = Path(temporary)
        with connection(values) as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            # Never wait while retaining an old snapshot: concurrent checkpoint
            # index creation may itself be waiting for that snapshot to finish.
            for lock in (726593927601, 726593927602):
                if not conn.execute('SELECT pg_try_advisory_xact_lock_shared(%s) AS acquired', (lock,)).fetchone()['acquired']:
                    raise RecoveryError('Schema migration is in progress; retry backup after it completes')
            server_policy(conn, development=development)
            version = schema_version(conn)
            snapshot = conn.execute('SELECT pg_export_snapshot() AS id, transaction_timestamp() AS at').fetchone()
            objects, files = object_inventory(conn, storage, root)
            tables = fingerprints(conn)
            dump = root / 'database.dump'
            utility(pg_dump, ['--format=custom', '--no-owner', '--no-privileges', '--no-password',
                '--snapshot=' + snapshot['id'], '--file=' + str(dump)], values, root)
            files['database.dump'] = digest(dump)
            manifest = {'format': 1, 'backup_id': secrets.token_hex(16), 'schema_version': version,
                'snapshot_at': snapshot['at'].isoformat(), 'source_bucket': storage.bucket,
                'source_provider': storage.provider,
                'server_version': conn.info.server_version, 'tables': tables, 'objects': objects, 'files': files}
        seal(root, manifest, destination, key)
        return {'backup_id': manifest['backup_id'], 'snapshot_at': manifest['snapshot_at'],
            'objects': len(objects), 'tables': len(tables), 'seconds': round(time.monotonic() - started, 3),
            'bundle': digest(destination)}


def verify(source, key, scratch):
    with tempfile.TemporaryDirectory(prefix='deepagent-verify-', dir=private_directory(scratch)) as temporary:
        manifest = unseal(source, Path(temporary), key)
        return {'backup_id': manifest['backup_id'], 'schema_version': manifest['schema_version'],
            'snapshot_at': manifest['snapshot_at'], 'objects': len(manifest['objects']), 'authenticated': True}


def restore(source, key, scratch, maintenance_dsn, storage, pg_restore, *, development=False):
    started = time.monotonic()
    values = connection_settings(maintenance_dsn, development=development)
    if storage.provider == 'local':
        raise RecoveryError('Restore requires a separate versioned recovery object store')
    with tempfile.TemporaryDirectory(prefix='deepagent-restore-', dir=private_directory(scratch)) as temporary:
        root = Path(temporary)
        manifest = unseal(source, root, key)
        if manifest['schema_version'] != LATEST_SCHEMA_VERSION or storage.bucket == manifest['source_bucket']:
            raise RecoveryError('Restore requires matching application schema and a different recovery bucket')
        if not re.fullmatch('[a-f0-9]{32}', manifest['backup_id']):
            raise RecoveryError('Invalid backup identity')
        for item in manifest['objects']:
            if item['table'] not in TABLES or item['file'] not in manifest['files'] or not item['file'].startswith('objects/'):
                raise RecoveryError('Invalid immutable object recovery inventory')
        # The destination name is always generated here; no existing DB can be selected.
        target = 'deepagent_restore_' + secrets.token_hex(12)
        marker = QUARANTINE + manifest['backup_id']
        restored = {**values, 'dbname': target}
        created = False
        try:
            with connection(values, autocommit=True) as admin:
                server_policy(admin, development=development, version=manifest['server_version'])
                admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0 ALLOW_CONNECTIONS false').format(sql.Identifier(target)))
                created = True
                admin.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(target), sql.Literal(marker)))
                admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(target)))
                admin.execute(sql.SQL('ALTER DATABASE {} SET default_transaction_read_only=on').format(sql.Identifier(target)))
                admin.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(target)))
            utility(pg_restore, ['--exit-on-error', '--single-transaction', '--no-owner', '--no-privileges',
                '--no-password', '--dbname=service=recovery', str(root / 'database.dump')], restored, root)
            with connection(restored) as conn:
                if fingerprints(conn) != manifest['tables']:
                    raise RecoveryError('Restored database/checkpoint content differs from the backup snapshot')
                schema_version(conn)
                content_index = {}
                for item in manifest['objects']:
                    identity = (item['key'], item['version'])
                    previous = content_index.setdefault(identity, item['file'])
                    if previous != item['file']:
                        raise RecoveryError('Ambiguous immutable object in recovery inventory')
                class BundleStore:
                    provider = manifest['source_provider']
                    bucket = manifest['source_bucket']

                    def get_content(self, key, version_id=None):
                        name = content_index.get((key, version_id))
                        if not version_id or name is None:
                            raise RecoveryError('Missing or ambiguous immutable object in recovery inventory')
                        return (root / name).read_bytes()

                expected, _ = object_inventory(conn, BundleStore(), root, materialize=False)
                if expected != manifest['objects']:
                    raise RecoveryError('Recovery object inventory does not match the restored database')
            replacements = []
            for item in manifest['objects']:
                content = (root / item['file']).read_bytes()
                metadata = storage.put_content(item['key'], content, item['content_type'])
                if (not metadata.version_id or metadata.bucket != storage.bucket or metadata.object_key != item['key']
                        or metadata.size_bytes != len(content)):
                    raise RecoveryError('Recovery object write did not attest a fixed version and size')
                actual = storage.get_content(item['key'], metadata.version_id)
                if hashlib.sha256(actual).hexdigest() != manifest['files'][item['file']]['sha256']:
                    raise RecoveryError('Recovered object failed fixed-version readback verification')
                replacements.append((item, metadata))
            with connection(restored) as conn:
                conn.execute('SET TRANSACTION READ WRITE')
                for item, metadata in replacements:
                    if item['table'] == 'knowledge_document_versions':
                        conn.execute('UPDATE knowledge_document_versions SET object_version_id=%s,etag=%s WHERE id=%s',
                            (metadata.version_id, metadata.etag, item['id']))
                    else:
                        uri = f"snapshot-object://{quote(storage.bucket, safe='')}/{item['key']}?" + urlencode({'version': metadata.version_id})
                        conn.execute(sql.SQL('UPDATE {} SET archive_path=%s WHERE id=%s').format(sql.Identifier(item['table'])),
                            (uri, item['id']))
                # Pending uploads also point only at the recovery store; original signed URLs are never replayed.
                conn.execute("""UPDATE knowledge_document_versions SET bucket=%s,region=%s,storage_provider=%s,
                    canonical_uri=%s || object_key""", (storage.bucket, storage.region, storage.provider, f'oss://{storage.bucket}/'))
            return {'backup_id': manifest['backup_id'], 'database': target, 'status': 'QUARANTINED',
                'objects': len(replacements), 'snapshot_at': manifest['snapshot_at'],
                'restore_seconds': round(time.monotonic() - started, 3),
                'snapshot_age_seconds': round((datetime.now(timezone.utc) - datetime.fromisoformat(manifest['snapshot_at'])).total_seconds(), 3)}
        except Exception:
            # Never DROP a restored DB or delete transferred object versions after a failed check.
            if created:
                raise RecoveryError('Recovery failed; retained quarantined or connection-disabled database: ' + target) from None
            raise RecoveryError('Recovery destination could not be created; no existing database was modified') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['backup', 'verify', 'restore'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--database-file', type=Path)
    parser.add_argument('--postgres-bin', type=Path)
    parser.add_argument('--storage-config', type=Path)
    parser.add_argument('--development-loopback', action='store_true')
    args = parser.parse_args()
    key = read_private_file(args.key_file, 32)
    if args.operation == 'verify':
        report = verify(args.bundle, key, args.scratch)
    else:
        if not all((args.database_file, args.postgres_bin, args.storage_config)):
            parser.error('backup/restore requires database-file, postgres-bin and storage-config')
        from packages.knowledge.storage.oss import AliyunOSSObjectStorage
        settings = json.loads(args.storage_config.read_text())
        if set(settings) != {'bucket', 'region', 'endpoint'} or not settings['endpoint'].startswith('https://'):
            raise RecoveryError('Use an explicit HTTPS recovery storage configuration')
        storage = AliyunOSSObjectStorage(**settings)
        dsn = read_private_file(args.database_file, 16384).decode().strip()
        if args.operation == 'backup':
            report = backup(dsn, storage, args.bundle, key, args.scratch, args.postgres_bin / 'pg_dump', development=args.development_loopback)
        else:
            report = restore(args.bundle, key, args.scratch, dsn, storage, args.postgres_bin / 'pg_restore', development=args.development_loopback)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    from packages.operations.logging import configure_logging
    configure_logging()
    try:
        main()
    except Exception as error:
        # No traceback, libpq diagnostics, source content, DSN or key material in CLI output.
        print(str(error) if isinstance(error, RecoveryError) else 'Recovery operation failed; inspect private configuration', file=__import__('sys').stderr)
        raise SystemExit(1) from None
