from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import replace
from datetime import timedelta
from threading import Event, Lock, BoundedSemaphore
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from packages.knowledge.errors import KnowledgeConflictError, KnowledgeStorageError
from packages.knowledge.models import KnowledgeBaseCreate, UploadPrepare, UploadComplete
from packages.knowledge.service import KnowledgeService
from packages.knowledge.storage.oss import AliyunOSSObjectStorage
from packages.knowledge.upload_governance import UploadSettings
from packages.persistence import create_database
from packages.runtime.admission import CapacityExceeded
from test_runtime_concurrency import runtime, race


def prepared(runtime, label='one'):
    _, services, context, *_ = runtime
    service = services.knowledge
    base = service.create_knowledge_base(KnowledgeBaseCreate(name='Upload governance ' + label), context)
    content = ('Upload governance evidence ' + label).encode()
    payload = UploadPrepare(filename=label + '.txt', content_type='text/plain',
        size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    upload = service.prepare_upload(base['id'], payload, context, 'intent-' + label)
    service.upload_content(upload['document_version_id'], content, 'text/plain', context)
    return upload, payload, base


def completion(runtime, upload):
    _, services, context, *_ = runtime
    return asyncio.run(services.knowledge.complete_upload(upload['document_version_id'], UploadComplete(), context))


def test_capacity_rejection_and_completion_never_download_or_scan_in_api(runtime, monkeypatch):
    _, services, context, *_ = runtime
    upload, _, _ = prepared(runtime)
    knowledge = services.knowledge

    def forbidden(*args, **kwargs):
        raise AssertionError('Content IO must only run in an admitted worker')

    monkeypatch.setattr(knowledge.storage, 'get_content', forbidden)
    monkeypatch.setattr(knowledge, 'content_scanner', SimpleNamespace(scan=forbidden))
    job = completion(runtime, upload)
    assert job['status'] == 'QUEUED'
    monkeypatch.setattr(knowledge.storage, 'head_object', forbidden)
    assert completion(runtime, upload)['id'] == job['id']
    knowledge.admission.settings = replace(knowledge.admission.settings, knowledge_user_active=0)
    services.db.execute("UPDATE knowledge_ingestion_jobs SET status='FAILED' WHERE id=?", (job['id'],))
    with pytest.raises(CapacityExceeded):
        completion(runtime, upload)
    assert services.db.fetch_one('SELECT status FROM knowledge_ingestion_jobs WHERE id=?', (job['id'],))['status'] == 'FAILED'


def test_metadata_concurrency_rejects_before_provider_io(runtime, monkeypatch):
    _, services, *_ = runtime
    upload, _, _ = prepared(runtime)
    services.knowledge._metadata_slots = BoundedSemaphore(0)
    def forbidden(*args, **kwargs):
        raise AssertionError('Provider HEAD must not start without an available slot')
    monkeypatch.setattr(services.knowledge.storage, 'head_object', forbidden)
    with pytest.raises(CapacityExceeded, match='metadata'):
        completion(runtime, upload)
    assert services.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs')['n'] == 0


def test_completion_replay_can_omit_optional_hints_but_not_change_them(runtime):
    _, services, context, *_ = runtime
    upload, _, _ = prepared(runtime)
    version = services.db.fetch_one('SELECT object_key FROM knowledge_document_versions WHERE id=?', (upload['document_version_id'],))
    metadata = services.knowledge.storage.head_object(version['object_key'])
    first = asyncio.run(services.knowledge.complete_upload(upload['document_version_id'], UploadComplete(etag=metadata.etag), context))
    assert completion(runtime, upload)['id'] == first['id']
    with pytest.raises(KnowledgeConflictError):
        asyncio.run(services.knowledge.complete_upload(upload['document_version_id'], UploadComplete(etag='changed'), context))


def test_independent_workers_and_completion_replays_scan_a_version_once(runtime, monkeypatch):
    _, services, context, _, location = runtime
    upload, _, _ = prepared(runtime)
    databases = [create_database(location) for _ in range(4)]
    peers = [KnowledgeService(db, services.knowledge.storage, services.knowledge.embedding) for db in databases]
    counter, lock = {'downloads': 0, 'scans': 0}, Lock()
    original = services.knowledge.storage.get_content

    def read(*args, **kwargs):
        with lock:
            counter['downloads'] += 1
        return original(*args, **kwargs)

    def scan(*args, **kwargs):
        with lock:
            counter['scans'] += 1

    monkeypatch.setattr(services.knowledge.storage, 'get_content', read)
    for peer in peers:
        peer.content_scanner = SimpleNamespace(scan=scan)
    try:
        jobs = race(lambda index: asyncio.run(peers[index].complete_upload(
            upload['document_version_id'], UploadComplete(), context)), count=len(peers))
        assert len({job['id'] for job in jobs}) == 1
        assert counter == {'downloads': 0, 'scans': 0}
        race(lambda index: asyncio.run(peers[index]._process_job(jobs[0]['id'])), count=len(peers))
        assert counter == {'downloads': 1, 'scans': 1}
        assert services.knowledge.get_ingestion_job(jobs[0]['id'], context)['status'] == 'SUCCEEDED'
    finally:
        for db in databases:
            db.close()


@pytest.mark.parametrize('field', ['tenant_bytes', 'project_bytes', 'user_bytes',
                                  'tenant_pending', 'project_pending', 'user_pending'])
def test_upload_reservations_are_atomic_and_count_retained_data(runtime, field):
    _, services, context, *_ = runtime
    service = services.knowledge
    base = service.create_knowledge_base(KnowledgeBaseCreate(name='Bounded preparation'), context)
    service.uploads.settings = replace(service.uploads.settings, **{field: 7 if field.endswith('_bytes') else 1})
    payload = UploadPrepare(filename='quota.txt', content_type='text/plain', size_bytes=7,
        sha256=hashlib.sha256(b'fixture').hexdigest())

    def attempt(index):
        try:
            return service.prepare_upload(base['id'], payload, context, 'quota-' + str(index))
        except CapacityExceeded:
            return None

    results = race(attempt, count=5)
    assert sum(result is not None for result in results) == 1
    assert services.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_document_versions')['n'] == 1
    assert services.db.fetch_one('SELECT COUNT(*) AS n FROM idempotency_records WHERE key LIKE ?', ('quota-%',))['n'] == 1


@pytest.mark.parametrize('field', ['global_running', 'tenant_running', 'project_running', 'user_running'])
def test_running_slots_are_cross_connection_atomic_and_released_after_failure(runtime, field):
    _, services, _, _, location = runtime
    jobs = [completion(runtime, prepared(runtime, str(index))[0]) for index in range(3)]
    databases = [create_database(location) for _ in jobs]
    peers = [KnowledgeService(db, services.knowledge.storage, services.knowledge.embedding) for db in databases]
    for peer in peers:
        peer.uploads.settings = replace(peer.uploads.settings, **{field: 1})
    try:
        claimed = race(lambda index: peers[index]._claim_job(jobs[index]['id'], 'synthetic-lease'), count=3)
        assert sum(claimed) == 1
        winner = claimed.index(True)
        next_index = (winner + 1) % len(jobs)
        services.db.execute("UPDATE knowledge_ingestion_jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id=?", (jobs[winner]['id'],))
        assert not peers[next_index]._claim_job(jobs[next_index]['id'], 'second-lease')
        services.db.execute("UPDATE knowledge_ingestion_jobs SET status='FAILED' WHERE id=?", (jobs[winner]['id'],))
        assert peers[next_index]._claim_job(jobs[next_index]['id'], 'second-lease')
    finally:
        for db in databases:
            db.close()


def test_expired_upload_cannot_be_renewed_and_keeps_its_byte_charge(runtime, monkeypatch):
    _, services, context, *_ = runtime
    upload, payload, base = prepared(runtime)
    service, db = services.knowledge, services.db
    service.uploads.settings = replace(service.uploads.settings, user_bytes=payload.size_bytes, user_pending=1)
    db.execute('UPDATE knowledge_document_versions SET upload_expires_at=? WHERE id=?',
        ((db.current_time() - timedelta(seconds=1)).isoformat(), upload['document_version_id']))
    with pytest.raises(KnowledgeConflictError, match='expired'):
        service.prepare_upload(base['id'], payload, context, 'intent-one')
    with pytest.raises(KnowledgeConflictError, match='expired'):
        completion(runtime, upload)
    asyncio.run(service.reconcile())
    assert db.fetch_one('SELECT status FROM knowledge_document_versions WHERE id=?', (upload['document_version_id'],))['status'] == 'EXPIRED'
    with pytest.raises(CapacityExceeded, match='retained-byte'):
        service.prepare_upload(base['id'], payload, context, 'new-intent')
    service.uploads.settings = replace(service.uploads.settings, user_bytes=2 * payload.size_bytes)
    assert service.prepare_upload(base['id'], payload, context, 'new-intent')['status'] == 'PENDING_UPLOAD'


def test_completion_payload_is_bound_and_rejected_content_never_publishes(runtime):
    _, services, context, *_ = runtime
    upload, _, _ = prepared(runtime)
    job = completion(runtime, upload)
    with pytest.raises(KnowledgeConflictError, match='different content'):
        asyncio.run(services.knowledge.complete_upload(upload['document_version_id'], UploadComplete(etag='another'), context))
    from packages.content_security import ContentRejectedError
    class Scanner:
        def scan(self, *args, **kwargs):
            raise ContentRejectedError('Rejected by the synthetic test scanner')
    services.knowledge.content_scanner = Scanner()
    asyncio.run(services.knowledge._process_job(job['id']))
    assert services.knowledge.get_ingestion_job(job['id'], context)['status'] == 'FAILED'
    assert services.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_chunks')['n'] == 0
    with pytest.raises(KnowledgeConflictError):
        services.knowledge.download_document_version(upload['document_version_id'], context)


def test_schema20_upgrade_preserves_legacy_sources_and_does_not_renew_expiry(runtime):
    _, services, *_ = runtime
    upload, _, _ = prepared(runtime)
    db = services.db
    db.execute('UPDATE knowledge_document_versions SET upload_expires_at=NULL WHERE id=?', (upload['document_version_id'],))
    db.execute('DELETE FROM schema_migrations WHERE version=20')
    db.initialize()
    before = db.fetch_one('SELECT * FROM knowledge_document_versions WHERE id=?', (upload['document_version_id'],))
    assert before['upload_expires_at'] and before['upload_request_hash'] is None
    db.initialize()
    assert db.fetch_one('SELECT * FROM knowledge_document_versions WHERE id=?', (upload['document_version_id'],)) == before


async def test_cancelled_native_io_is_owned_until_the_thread_really_finishes():
    entered, release = Event(), Event()
    def native():
        entered.set()
        assert release.wait(5)
    task = asyncio.create_task(KnowledgeService._owned_io(native))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_worker_cancellation_drains_download_before_fencing_and_recovery(runtime, monkeypatch):
    _, services, context, _, location = runtime
    upload, _, _ = prepared(runtime)
    job = completion(runtime, upload)
    service = services.knowledge
    entered, release = Event(), Event()
    original = service.storage.get_content
    reads = []

    def slow_read(*args, **kwargs):
        reads.append(args)
        if len(reads) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.storage, 'get_content', slow_read)
    peer_db = create_database(location)
    peer = KnowledgeService(peer_db, service.storage, service.embedding)
    async def exercise():
        task = asyncio.create_task(service._process_job(job['id']))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            row = services.db.fetch_one('SELECT status,heartbeat_at FROM knowledge_ingestion_jobs WHERE id=?', (job['id'],))
            assert row['status'] == 'RUNNING'
            assert row['heartbeat_at'] > (services.db.current_time() - timedelta(seconds=service.lease_seconds)).isoformat()
            assert not peer._claim_job(job['id'], 'not-authorized-yet')
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        await peer.reconcile()
        assert peer.get_ingestion_job(job['id'], context)['status'] == 'QUEUED'
        await peer._process_job(job['id'])
        final = peer.get_ingestion_job(job['id'], context)
        assert final['status'] == 'SUCCEEDED' and final['attempts'] == 2
        assert len(reads) == 2
        assert services.db.fetch_one('SELECT COUNT(*) AS n FROM knowledge_base_revisions')['n'] == 1
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        peer_db.close()


def test_real_oss_sdk_signs_exact_upload_length_without_network(monkeypatch):
    import alibabacloud_oss_v2 as oss
    monkeypatch.setattr('packages.knowledge.storage.oss.create_oss_credentials_provider',
        lambda module: oss.credentials.StaticCredentialsProvider('synthetic-id', 'synthetic-secret'))
    store = AliyunOSSObjectStorage('synthetic-bucket', 'cn-beijing', 'https://oss-cn-beijing.aliyuncs.com')
    authorization = store.create_upload_authorization('test/document', 'text/plain', size_bytes=7)
    assert authorization.headers['Content-Length'] == '7'
    query = parse_qs(urlsplit(authorization.url).query)
    assert 'content-length' in query['x-oss-additional-headers'][0].split(';')
    assert authorization.method == 'PUT'
    with pytest.raises(KnowledgeStorageError):
        store.create_upload_authorization('test/document', 'text/plain', size_bytes=0)


def test_oss_download_deadline_closes_response(monkeypatch):
    body = io.BytesIO(b'content')
    store = AliyunOSSObjectStorage('test', 'cn-beijing')
    store._oss = SimpleNamespace(GetObjectRequest=lambda **kwargs: kwargs)
    store._sdk_client = SimpleNamespace(get_object=lambda *args: SimpleNamespace(body=body))
    ticks = iter([0, 1, 61])
    monkeypatch.setattr('packages.knowledge.storage.oss.monotonic', lambda: next(ticks))
    with pytest.raises(KnowledgeStorageError, match='deadline'):
        store.get_content('test', 'fixed-version')
    assert body.closed


def test_scanner_slow_response_cannot_extend_its_total_deadline(monkeypatch):
    from packages.content_security.scanner import ClamAVContentScanner, ContentScanError
    clock = [0]
    class Socket:
        closed = False
        def settimeout(self, seconds):
            assert 0 < seconds <= 1
        def sendall(self, data):
            pass
        def recv(self, size):
            clock[0] += 2
            return b'stream: OK\0'
        def close(self):
            self.closed = True
    connection = Socket()
    monkeypatch.setattr('packages.content_security.scanner.monotonic', lambda: clock[0])
    monkeypatch.setattr('socket.create_connection', lambda *args, **kwargs: connection)
    with pytest.raises(ContentScanError, match='unavailable'):
        ClamAVContentScanner(timeout_seconds=1).scan(b'test', object_name='synthetic')
    assert connection.closed


@pytest.mark.parametrize('value', [-1, True, 1.5, 2**51])
def test_upload_limits_fail_closed(value):
    with pytest.raises(ValueError):
        UploadSettings(tenant_bytes=value)
