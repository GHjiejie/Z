import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from packages.operations import disaster_recovery as recovery
from packages.operations import recovery_bundle as bundle
from packages.persistence import create_database
from packages.auth.service import AuthService
from packages.runtime.checkpoint_saver import FencedCheckpointSaver
from packages.persistence.archive_store import SharedArchiveStore


class VersionedStore:
    provider = 'aliyun_oss'
    region = 'synthetic-region'

    def __init__(self, bucket='source-fixture'):
        self.bucket = bucket
        self.objects = {}

    def put_content(self, key, content, content_type):
        from packages.knowledge.ports import ObjectMetadata
        version = secrets.token_hex(8)
        self.objects[(key, version)] = bytes(content)
        return ObjectMetadata(self.bucket, self.region, key, len(content), content_type, version_id=version)

    def get_content(self, key, version_id=None):
        assert version_id, 'Never read a mutable latest version'
        return self.objects[key, version_id]


@pytest.fixture
def directories(tmp_path):
    scratch, output = tmp_path / 'scratch', tmp_path / 'output'
    scratch.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    return scratch, output


@pytest.fixture
def bundle_fixture(directories):
    scratch, output = directories
    root = scratch / 'build'
    root.mkdir(mode=0o700)
    bundle.private_write(root / 'database.dump', b'synthetic-database-private')
    manifest = {'format': 1, 'backup_id': '1'*32, 'schema_version': 19, 'snapshot_at': '2026-08-31T00:00:00+00:00',
        'objects': [], 'files': {'database.dump': bundle.digest(root / 'database.dump')}}
    target, key = output / 'backup.dagbackup', secrets.token_bytes(32)
    bundle.seal(root, manifest, target, key)
    return target, key, scratch, manifest


def test_bundle_authentication_no_plaintext_and_no_overwrite(bundle_fixture):
    target, key, scratch, manifest = bundle_fixture
    assert b'synthetic-database-private' not in target.read_bytes()
    assert b'backup_id' not in target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    assert recovery.verify(target, key, scratch)['authenticated']
    output = scratch / 'read'
    output.mkdir(mode=0o700)
    assert bundle.unseal(target, output, key) == manifest
    assert (output / 'database.dump').read_bytes() == b'synthetic-database-private'
    with pytest.raises(bundle.RecoveryError, match='overwrite'):
        bundle.seal(scratch / 'build', manifest, target, key)


@pytest.mark.parametrize('mutation', ['header', 'nonce', 'body', 'tag', 'truncated', 'appended', 'wrong-key'])
def test_tampered_bundle_cannot_be_consumed(bundle_fixture, mutation):
    target, key, scratch, _ = bundle_fixture
    data = bytearray(target.read_bytes())
    if mutation == 'wrong-key':
        key = secrets.token_bytes(32)
    elif mutation == 'truncated':
        data = data[:-1]
    elif mutation == 'appended':
        data.extend(b'X')
    else:
        positions = {'header':0, 'nonce':len(bundle.MAGIC), 'body':len(bundle.MAGIC)+14, 'tag':len(data)-1}
        data[positions[mutation]] ^= 1
    target.write_bytes(data)
    before = set(scratch.iterdir())
    with pytest.raises(bundle.RecoveryError):
        recovery.verify(target, key, scratch)
    assert set(scratch.iterdir()) == before, 'Failed decrypt temporary plaintext must be cleaned'


@pytest.mark.parametrize('mutation', ['duplicate', 'traversal', 'symlink', 'missing', 'content'])
def test_authenticated_but_unsafe_archive_is_not_extracted(bundle_fixture, mutation):
    import io
    import tarfile
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    target, key, scratch, manifest = bundle_fixture
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:') as archive:
        def add(name, data, link=False):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if link:
                member.type, member.linkname, member.size = tarfile.SYMTYPE, '../outside', 0
            archive.addfile(member, io.BytesIO(data))
        add('manifest.json', json.dumps(manifest).encode())
        if mutation != 'missing':
            add('database.dump', b'changed-but-authenticated' if mutation == 'content' else b'synthetic-database-private', mutation == 'symlink')
        if mutation == 'duplicate':
            add('database.dump', b'synthetic-database-private')
        elif mutation == 'traversal':
            add('../outside', b'escape')
    nonce = secrets.token_bytes(12)
    header = bundle.MAGIC + nonce
    target.write_bytes(header + AESGCM(key).encrypt(nonce,output.getvalue(),header))
    with pytest.raises(bundle.RecoveryError):
        recovery.verify(target,key,scratch)
    assert not (scratch / 'outside').exists()


@pytest.mark.parametrize('fault', ['symlink', 'public-file', 'oversize', 'public-dir'])
def test_recovery_private_files_and_directories_fail_closed(tmp_path, fault):
    target = tmp_path / 'key'
    bundle.private_write(target, b'x' * (33 if fault == 'oversize' else 32))
    if fault == 'symlink':
        link = tmp_path / 'link'
        link.symlink_to(target)
        target = link
    elif fault == 'public-file':
        target.chmod(0o644)
    elif fault == 'public-dir':
        tmp_path.chmod(0o755)
    with pytest.raises((bundle.RecoveryError, OSError)):
        if fault == 'public-dir':
            bundle.private_directory(tmp_path)
        else:
            bundle.read_private_file(target, 32)


@pytest.mark.parametrize('dsn,development', [
    ('postgresql://user@external/db', True), ('postgresql://user@127.0.0.1/db', False),
    ('dbname=db user=user', True), ('postgresql://user@127.0.0.1/db?options=-csearch_path%3Dprivate', True),
    ('postgresql://user@127.0.0.1/db?hostaddr=10.0.0.1', True),
])
def test_recovery_database_connection_policy(dsn, development):
    with pytest.raises(bundle.RecoveryError):
        recovery.connection_settings(dsn, development=development)


def test_native_credentials_are_not_in_argv_or_inherited_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('PGPASSWORD', 'untrusted-password')
    monkeypatch.setenv('PGOPTIONS', '-csearch_path=private')
    values = {'host':'127.0.0.1','dbname':'owned','user':'owner','password':'private-secret'}
    with recovery.utility_environment(values, tmp_path) as environment:
        assert 'PGPASSWORD' not in environment and 'private-secret' not in repr(environment)
        path = Path(environment['PGSERVICEFILE'])
        assert path.stat().st_mode & 0o777 == 0o600
        assert 'password=private-secret' in path.read_text()
        assert 'search_path' not in environment['PGOPTIONS']
    assert not path.exists()
    with pytest.raises(bundle.RecoveryError, match='ambient'):
        recovery.connection_settings('postgresql://owner@127.0.0.1/db', development=True)


@pytest.fixture
def source_database(directories, monkeypatch):
    admin_dsn = os.getenv('DEEPAGENT_TEST_POSTGRES_URL')
    if not admin_dsn:
        pytest.skip('DEEPAGENT_TEST_POSTGRES_URL is required')
    dump, restore = shutil.which('pg_dump'), shutil.which('pg_restore')
    if not dump or not restore:
        pytest.skip('Real PostgreSQL dump/restore tools are required')
    source_name = 'deepagent_backup_test_' + secrets.token_hex(10)
    owned = [source_name]
    original_restore = recovery.restore

    def tracked_restore(*args, **kwargs):
        try:
            return original_restore(*args, **kwargs)
        except bundle.RecoveryError as error:
            match = re.search(r'(deepagent_restore_[a-f0-9]{24})$', str(error))
            if match and match[1] not in owned:
                owned.append(match[1])
            raise

    monkeypatch.setattr(recovery, 'restore', tracked_restore)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(source_name)))
        parts = urlsplit(admin_dsn)
        dsn = urlunsplit(parts._replace(path='/' + source_name, query=''))
        db = create_database(dsn)
        try:
            db.initialize()
            FencedCheckpointSaver(db).initialize(auto_migrate=True)
            auth = AuthService(db)
            auth.bootstrap_super_admin('Synthetic-Backup1!')
            auth.login('admin', 'Synthetic-Backup1!')
            from langgraph.checkpoint.base import empty_checkpoint
            from langgraph.checkpoint.postgres import PostgresSaver
            with db.pool.connection() as conn:
                saver = PostgresSaver(conn)
                checkpoint = empty_checkpoint()
                checkpoint['channel_values'] = {'fixture': b'private-checkpoint-bytes'}
                checkpoint['channel_versions'] = {'fixture': '1'}
                config = saver.put({'configurable': {'thread_id':'owned-fixture','checkpoint_ns':''}}, checkpoint,
                    {'source':'input','step':0,'parents':{}}, {'fixture':'1'})
                saver.put_writes(config, [('pending', 'private-pending-write')], 'fixture-task')
            yield SimpleNamespace(db=db, dsn=dsn, admin_dsn=admin_dsn, dump=dump, restore=restore,
                owned=owned, scratch=directories[0], output=directories[1], checkpoint=config)
        finally:
            db.close()
            for name in owned:
                assert re.fullmatch(r'deepagent_(backup_test_[a-f0-9]{20}|restore_[a-f0-9]{24})', name)
                # Exact names created by this fixture or returned by its restore call only.
                admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def add_objects(source):
    db, storage = source.db, VersionedStore()
    content = b'private-document\x00binary'
    metadata = storage.put_content('documents/fixture', content, 'application/octet-stream')
    checksum = hashlib.sha256(content).hexdigest()
    now = db.current_time().isoformat()
    db.execute("INSERT INTO knowledge_bases(id,tenant_id,project_id,name,created_at,updated_at) VALUES('kb','t','p','fixture',?,?)", (now,now))
    db.execute("""INSERT INTO knowledge_documents(id,knowledge_base_id,tenant_id,project_id,display_name,created_by,created_at,updated_at)
        VALUES('doc','kb','t','p','fixture','synthetic-user',?,?)""", (now,now))
    db.execute("""INSERT INTO knowledge_document_versions(id,document_id,tenant_id,project_id,revision_number,storage_provider,
        bucket,region,object_key,object_version_id,canonical_uri,content_sha256,content_type,size_bytes,expected_size_bytes,status,created_at)
        VALUES('version','doc','t','p',1,?,?,?,?,?,?,?,'application/octet-stream',?,?,'INDEXED',?)""",
        (storage.provider,storage.bucket,storage.region,metadata.object_key,metadata.version_id,'oss://source-fixture/documents/fixture',checksum,len(content),len(content),now))
    db.execute("""INSERT INTO repositories(id,tenant_id,project_id,name,provider,canonical_uri,default_branch,access_policy_revision_id,status,created_at,updated_at)
        VALUES('repo','t','p','fixture','local_snapshot','private-fixture','main','policy','ACTIVE',?,?)""", (now,now))
    archive = b'private-source-archive'
    uri = SharedArchiveStore(storage).put(archive, tenant_id='t',project_id='p',kind='repository')
    db.execute("""INSERT INTO repository_snapshots(id,repository_id,tenant_id,project_id,requested_ref,resolved_commit_sha,source_mode,
        manifest_hash,archive_path,archive_sha256,size_bytes,file_count,created_at)
        VALUES('snapshot','repo','t','p','main','commit','worktree','manifest',?,?,?,1,?)""",
        (uri,hashlib.sha256(archive).hexdigest(),len(archive),now))
    from packages.knowledge.embedding import HashEmbeddingProvider
    from packages.knowledge.service import KnowledgeService
    from packages.domain.models import TenantContext
    embedding = HashEmbeddingProvider()
    text = 'Verified recovery fixture with document citations'
    db.execute("""INSERT INTO knowledge_chunks(id,tenant_id,project_id,knowledge_base_id,document_id,document_version_id,
        position,text,token_count,content_hash,locator_json,embedding_json,created_at)
        VALUES('chunk','t','p','kb','doc','version',0,?,6,?,'{}',?,?)""",
        (text,hashlib.sha256(text.encode()).hexdigest(),db.encode(embedding.embed_query(text)),now))
    db.execute("UPDATE knowledge_documents SET status='READY',current_version_id='version' WHERE id='doc'")
    db.execute("UPDATE knowledge_document_versions SET status='READY' WHERE id='version'")
    KnowledgeService(db,storage,embedding)._publish_revision('kb',TenantContext(tenant_id='t',project_id='p'))
    return storage


def test_real_consistent_dump_restore_checkpoint_objects_and_quarantine(source_database, monkeypatch):
    source = source_database
    storage = add_objects(source)
    original_versions = dict(storage.objects)
    key, target = secrets.token_bytes(32), source.output / 'fixture.dagbackup'
    original = recovery.utility

    def concurrent_write(binary, arguments, values, root):
        if Path(binary).name == 'pg_dump':
            source.db.execute("UPDATE users SET display_name='after-snapshot' WHERE username='admin'")
        return original(binary, arguments, values, root)

    monkeypatch.setattr(recovery, 'utility', concurrent_write)
    report = recovery.backup(source.dsn, storage, target, key, source.scratch, source.dump, development=True)
    assert report['objects'] == 2
    assert recovery.verify(target, key, source.scratch)['authenticated']
    assert source.db.fetch_one("SELECT display_name FROM users WHERE username='admin'")['display_name'] == 'after-snapshot'
    assert storage.objects == original_versions
    recovered_store = VersionedStore('recovery-fixture')
    restored = recovery.restore(target,key,source.scratch,source.admin_dsn,recovered_store,source.restore,development=True)
    source.owned.append(restored['database'])
    assert restored['status'] == 'QUARANTINED' and restored['objects'] == 2
    url = urlunsplit(urlsplit(source.dsn)._replace(path='/' + restored['database']))
    db = create_database(url)
    try:
        assert db.fetch_one("SELECT display_name FROM users WHERE username='admin'")['display_name'] != 'after-snapshot'
        assert db.fetch_one('SELECT COUNT(*) AS count FROM auth_sessions')['count'] == 1
        assert db.fetch_one('SHOW default_transaction_read_only')['default_transaction_read_only'] == 'on'
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("UPDATE users SET display_name='should fail'")
        with pytest.raises(bundle.RecoveryError, match='quarantined'):
            recovery.assert_not_recovery_database(db)
        from langgraph.checkpoint.postgres import PostgresSaver
        with db.pool.connection() as conn:
            checkpoint = PostgresSaver(conn).get_tuple(source.checkpoint)
            assert checkpoint.checkpoint['channel_values']['fixture'] == b'private-checkpoint-bytes'
            assert checkpoint.pending_writes[0][2] == 'private-pending-write'
        row = db.fetch_one("SELECT * FROM knowledge_document_versions WHERE id='version'")
        assert row['bucket'] == recovered_store.bucket
        assert recovered_store.get_content(row['object_key'],row['object_version_id']) == b'private-document\x00binary'
        snapshot = db.fetch_one("SELECT * FROM repository_snapshots WHERE id='snapshot'")
        assert SharedArchiveStore(recovered_store).read(snapshot, kind='repository') == b'private-source-archive'
        from packages.knowledge.embedding import HashEmbeddingProvider
        from packages.knowledge.service import KnowledgeService
        knowledge = KnowledgeService(db,recovered_store,HashEmbeddingProvider())
        for revision in db.fetch_all('SELECT * FROM knowledge_base_revisions'):
            knowledge._verify_revision_index(revision)
    finally:
        db.close()
    from apps.platform_api.main import create_app
    from fastapi.testclient import TestClient
    with pytest.raises(bundle.RecoveryError, match='quarantined'):
        with TestClient(create_app(url, seed=False, load_env=False)):
            pytest.fail('API/Worker may not start on a restored database')


def test_missing_or_mutated_object_prevents_backup_publication(source_database):
    source = source_database
    storage = add_objects(source)
    key = next(iter(storage.objects))
    storage.objects[key] = b'tampered'
    destination = source.output / 'broken.dagbackup'
    with pytest.raises(bundle.RecoveryError):
        recovery.backup(source.dsn,storage,destination,secrets.token_bytes(32),source.scratch,source.dump,development=True)
    assert not destination.exists()
    assert list(source.scratch.iterdir()) == []


def test_source_bucket_restore_rejected_before_database_or_object_writes(source_database, monkeypatch):
    source = source_database
    storage = add_objects(source)
    key, target = secrets.token_bytes(32), source.output / 'fixture.dagbackup'
    recovery.backup(source.dsn,storage,target,key,source.scratch,source.dump,development=True)
    monkeypatch.setattr(recovery, 'connection', lambda *a,**k: pytest.fail('must reject before creating database'))
    with pytest.raises(bundle.RecoveryError, match='different recovery bucket'):
        recovery.restore(target,key,source.scratch,source.admin_dsn,storage,source.restore,development=True)


def test_failed_object_restore_preserves_quarantined_database_and_source(source_database, monkeypatch):
    source = source_database
    storage = add_objects(source)
    original = dict(storage.objects)
    key, target = secrets.token_bytes(32), source.output / 'fixture.dagbackup'
    recovery.backup(source.dsn,storage,target,key,source.scratch,source.dump,development=True)
    recovered = VersionedStore('recovery-fixture')
    monkeypatch.setattr(recovered, 'get_content', lambda *a: b'corrupted-at-destination')
    with pytest.raises(bundle.RecoveryError, match='retained quarantined') as failure:
        recovery.restore(target,key,source.scratch,source.admin_dsn,recovered,source.restore,development=True)
    name = str(failure.value).split(': ')[-1]
    if name not in source.owned:
        source.owned.append(name)
    with psycopg.connect(source.admin_dsn, autocommit=True) as admin:
        marker = admin.execute("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (name,)).fetchone()[0]
        assert marker.startswith(recovery.QUARANTINE)
    assert recovered.objects, 'Do not silently delete uploaded recovery versions after an error'
    assert storage.objects == original


def test_unauthenticated_restore_never_opens_database_or_storage(bundle_fixture, monkeypatch):
    target, _, scratch, _ = bundle_fixture
    monkeypatch.setattr(recovery, 'connection', lambda *a,**k: pytest.fail('must authenticate before database creation'))
    with pytest.raises(bundle.RecoveryError, match='authentication'):
        recovery.restore(target,secrets.token_bytes(32),scratch,'postgresql://owner@127.0.0.1/db',
            VersionedStore('recovery'),Path('/unused/pg_restore'),development=True)


@pytest.mark.parametrize('missing', ['checkpoint', 'object-version', 'local-archive'])
def test_incomplete_recovery_sources_are_not_reported_as_backups(source_database, missing):
    source = source_database
    storage = add_objects(source)
    if missing == 'checkpoint':
        source.db.execute('DELETE FROM checkpoint_migrations')
    elif missing == 'object-version':
        source.db.execute('UPDATE knowledge_document_versions SET object_version_id=NULL')
    else:
        source.db.execute("UPDATE repository_snapshots SET archive_path='/not-an-authorized-backup-source'")
    target = source.output / 'incomplete.dagbackup'
    from packages.coding.errors import CodingConflictError
    with pytest.raises((bundle.RecoveryError, CodingConflictError)):
        recovery.backup(source.dsn,storage,target,secrets.token_bytes(32),source.scratch,source.dump,development=True)
    assert not target.exists()
    assert list(source.scratch.iterdir()) == []


def test_backup_does_not_wait_with_a_snapshot_behind_checkpoint_migration(source_database):
    source = source_database
    target = source.output / 'during-migration.dagbackup'
    with psycopg.connect(source.dsn, autocommit=True) as owner:
        owner.execute('SELECT pg_advisory_lock(726593927602)')
        started = time.monotonic()
        with pytest.raises(bundle.RecoveryError, match='migration is in progress'):
            recovery.backup(source.dsn,VersionedStore(),target,secrets.token_bytes(32),source.scratch,source.dump,development=True)
        assert time.monotonic() - started < 2
        owner.execute('SELECT pg_advisory_unlock(726593927602)')
        assert owner.execute('SELECT pg_try_advisory_lock(726593927601)').fetchone()[0]
        owner.execute('SELECT pg_advisory_unlock(726593927601)')
    assert not target.exists()
