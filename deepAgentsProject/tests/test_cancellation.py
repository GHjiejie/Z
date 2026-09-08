import asyncio
import base64
import hashlib
import io
import json
import tarfile
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from deepagents.backends.protocol import ExecuteResponse
from fastapi.testclient import TestClient

from apps.sandbox_service.main import create_sandbox_service
from packages.coding.errors import CodingConflictError, SandboxUnavailableError
from packages.persistence.fencing import CancellationWriteFence, LeaseLostError, execution_scope
from packages.runtime.cancellation import CancellationFinalizer
from packages.runtime.coding_recovery import CodingRecovery
from packages.runtime.event_emitter import EventEmitter
from packages.persistence import create_database
from packages.runtime.run_lease import RunLeaseManager, finalize_cancellation
from packages.sandbox.cancellation_capture import CancellationCapture, capture_changes, validate_capture
from packages.sandbox.fake_provider import FakeSandboxProvider
from packages.sandbox.lease_authority import CancellationLease, ExecutionLease, SandboxExecutionGate
from packages.sandbox.ports import SandboxSnapshot
from packages.sandbox.remote_provider import RemoteSandboxProvider
from packages.coding.models import SandboxProfileSpec
from test_coding_recovery import coding_run, executor
from test_enterprise_isolation import _tar
from test_runtime_concurrency import runtime, race, new_run


@pytest.fixture
def cancelled(runtime, tmp_path):
    client, services, *_ = runtime
    provider = services.sandbox_manager.providers['fake']
    provider.command_handler = lambda command: ExecuteResponse(output='', exit_code=0)
    run, plan = coding_run(runtime, tmp_path)
    agent = executor(services)
    execution = RunLeaseManager(services.db, 'recovery-test').claim(run['id'])

    async def prepare():
        with execution_scope(execution):
            return await agent._prepare(run, plan)

    bound = client.portal.call(prepare)
    services.db.execute("UPDATE runs SET status='CANCELLING' WHERE id=?", (run['id'],))
    services.db.execute('UPDATE run_attempts SET lease_token=NULL,expires_at=NULL WHERE id=?', (execution.attempt_id,))
    yield SimpleNamespace(client=client, services=services, run=run, plan=plan, agent=agent,
        finalizer=CancellationFinalizer(agent), execution=execution, bound=bound, provider=provider, url=runtime[-1])


def finish(state):
    state.client.portal.call(state.finalizer.run, state.run['id'])
    finalize_cancellation(state.services.db, state.services.events, state.run['id'])


def artifacts(state):
    return state.services.db.fetch_all('SELECT id,name FROM artifacts WHERE run_id=? ORDER BY id', (state.run['id'],))


def test_cancellation_atomic_idempotent_and_old_agent_cannot_write(cancelled):
    state = cancelled
    finish(state)
    run = state.client.get('/api/v1/runs/' + state.run['id']).json()
    assert run['status'] == 'CANCELLED'
    assert run['cancellation']['status'] == 'COMPLETED'
    assert 'lease_token' not in json.dumps(run)
    first = artifacts(state)
    assert len(first) == 5 and len({item['name'] for item in first}) == 5
    assert {'changes.patch','diff.json','verification-report.json'}.issubset({item['name'] for item in first})
    report = state.services.db.fetch_one("SELECT content FROM artifacts WHERE run_id=? AND name='verification-report.json'", (state.run['id'],))
    assert json.loads(report['content'])['status'] == 'PARTIAL'
    assert state.client.get(f"/api/v1/runs/{state.run['id']}/verification").json()['status'] == 'PARTIAL'
    assert state.client.get(f"/api/v1/runs/{state.run['id']}/diff").json()['status'] == 'REVIEW_REQUIRED'
    finish(state)
    assert artifacts(state) == first
    with execution_scope(state.execution), pytest.raises(LeaseLostError):
        state.services.events.append(state.run['id'], 'stale.execution', {})
    point = state.services.db.fetch_one("SELECT * FROM coding_recovery_points WHERE run_id=?", (state.run['id'],))
    assert point['phase'] == 'CANCELLED'
    with pytest.raises(CodingConflictError, match='new Run'):
        CodingRecovery(state.services.db,state.services.events,state.services.sandbox_manager,
            state.services.checkpointer,state.run,state.plan).load()


def test_only_one_finalizer_claims_and_superseded_owner_cannot_commit(cancelled):
    state = cancelled
    claimed = [fence for fence in race(lambda _: state.finalizer.claim(state.run['id']), count=4) if fence]
    assert len(claimed) == 1
    old = claimed[0]
    past = (state.services.db.current_time() - timedelta(seconds=1)).isoformat()
    state.services.db.execute('UPDATE run_cancellations SET expires_at=? WHERE run_id=?', (past,state.run['id']))
    new = state.finalizer.claim(state.run['id'])
    assert new and new.lease_token != old.lease_token
    with execution_scope(old), pytest.raises(LeaseLostError):
        state.services.events.append(state.run['id'], 'stale.finalization', {})
    state.client.portal.call(state.finalizer._process, new)
    finalize_cancellation(state.services.db, state.services.events, state.run['id'])
    assert len(artifacts(state)) == 5


@pytest.mark.parametrize('failure', ['artifact', 'snapshot', 'recovery'])
def test_failure_rolls_back_all_evidence_and_retries_after_crash(cancelled, monkeypatch, failure):
    state = cancelled
    db = state.services.db
    original = state.services.events.append
    target = {'artifact':'artifact.created','snapshot':'workspace.snapshot.created','recovery':'graph.cancellation.sealed'}[failure]

    def reject(run_id, kind, *args, **kwargs):
        if kind == target:
            raise RuntimeError('sensitive-provider-secret')
        return original(run_id, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(state.services.events, 'append', reject)
        finish(state)
    assert not artifacts(state)
    assert not db.fetch_all('SELECT id FROM workspace_snapshots WHERE run_id=?', (state.run['id'],))
    assert not db.fetch_all('SELECT id FROM coding_recovery_points WHERE run_id=?', (state.run['id'],))
    result = state.client.get('/api/v1/runs/' + state.run['id']).json()
    assert result['status'] == 'CANCELLING' and result['cancellation']['status'] == 'PENDING'
    assert 'sensitive-provider-secret' not in json.dumps(result)
    past = (db.current_time()-timedelta(seconds=1)).isoformat()
    db.execute('UPDATE run_cancellations SET available_at=? WHERE run_id=?', (past,state.run['id']))
    # A replacement process uses durable state, not the failed object's memory.
    state.finalizer = CancellationFinalizer(executor(state.services))
    finish(state)
    assert len(artifacts(state)) == 5


def test_finalizer_survives_revoked_user_and_does_not_reenable_execution(cancelled):
    state = cancelled
    state.services.db.execute("UPDATE users SET status='INACTIVE' WHERE id=?", (state.run['principal_user_id'],))
    finish(state)
    assert len(artifacts(state)) == 5
    attempt = state.services.db.fetch_one('SELECT * FROM run_attempts WHERE id=?', (state.execution.attempt_id,))
    assert attempt['status'] == 'CANCELLED' and attempt['lease_token'] is None


def test_cancel_does_not_present_an_earlier_passed_verification_as_current(cancelled):
    state = cancelled
    previous = state.agent.verification.run(state.run, state.bound.workspace, state.bound.backend.raw,
        {'auto_discover': False, 'required_commands': ['true']})
    assert previous['status'] == 'PASSED'
    finish(state)
    current = state.client.get(f"/api/v1/runs/{state.run['id']}/verification").json()
    assert current['status'] == 'PARTIAL' and current['checks'] == []
    assert current['content_hash'] != previous['content_hash']
    assert current['summary']['reason'] == 'run_cancelled'


def test_next_run_restores_cancelled_files_but_no_old_graph(cancelled):
    state = cancelled
    raw = state.bound.backend.raw
    raw.upload_files([('/workspace/repo/counter.txt',b'cancelled final bytes\n'),
                      ('/artifacts/proof.bin',b'\x00\xffproof'),('/tmp/scratch',b'scratch')])
    finish(state)
    response = state.client.post(f"/api/v1/threads/{state.run['thread_id']}/runs", json={'input':'Continue from preserved files'})
    assert response.status_code == 202
    run = state.services.db.fetch_one('SELECT * FROM runs WHERE id=?', (response.json()['id'],))
    fence = RunLeaseManager(state.services.db, 'next-run').claim(run['id'])

    async def recover():
        with execution_scope(fence):
            restored = await state.agent._prepare(run, state.plan)
            assert restored.backend.raw.download_files(['/workspace/repo/counter.txt'])[0].content == b'cancelled final bytes\n'
            assert restored.backend.raw.download_files(['/artifacts/proof.bin'])[0].content == b'\x00\xffproof'
            assert restored.backend.raw.download_files(['/tmp/scratch'])[0].content == b'scratch'
            assert restored.recovery.source['graph']['records'] == []
            assert state.services.checkpointer.get_tuple({'configurable': {
                'thread_id': restored.recovery.session['graph_thread_id'], 'checkpoint_ns':''}}) is None
    state.client.portal.call(recover)


def test_cancel_snapshot_protected_from_ttl_cleanup(cancelled):
    state = cancelled
    past = (state.services.db.current_time()-timedelta(seconds=1)).isoformat()
    state.services.db.execute('UPDATE sandbox_instances SET expires_at=? WHERE id=?', (past,state.bound.sandbox_instance['id']))
    assert state.client.portal.call(state.services.sandbox_manager.destroy_expired) == 0
    assert state.bound.sandbox_instance['external_id'] in state.provider._backends


def _claim_in_child(url, run_id, channel):
    db = create_database(url)
    try:
        db.initialize(auto_migrate=False)
        finalizer = CancellationFinalizer(SimpleNamespace(db=db, events=EventEmitter(db), worker_id='killed-finalizer'))
        assert finalizer.claim(run_id)
        channel.send('claimed')
        channel.recv()
    finally:
        db.close()


def test_sigkill_finalizer_is_recovered_by_another_worker(cancelled):
    import multiprocessing
    state = cancelled
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_claim_in_child, args=(state.url,state.run['id'],child))
    process.start()
    try:
        assert parent.poll(15) and parent.recv() == 'claimed'
        process.kill()
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode != 0
        state.services.db.execute('UPDATE run_cancellations SET expires_at=? WHERE run_id=?',
            ((state.services.db.current_time()-timedelta(seconds=1)).isoformat(),state.run['id']))
        finish(state)
        assert len(artifacts(state)) == 5
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        parent.close()
        child.close()


def test_finalizer_renews_during_slow_capture(cancelled, monkeypatch):
    state = cancelled
    capture = state.provider.capture_cancellation
    async def slow(*args):
        await asyncio.sleep(1.25)
        state.services.db.assert_execution_fence()
        return await capture(*args)
    monkeypatch.setattr(state.provider, 'capture_cancellation', slow)
    state.finalizer.lease_seconds = 1
    finish(state)
    assert len(artifacts(state)) == 5


def test_late_archive_upload_cannot_publish_after_finalization_takeover(cancelled, monkeypatch):
    import contextvars
    state = cancelled
    manager = state.services.sandbox_manager
    prepare = manager.prepare_snapshot
    replacement = []
    async def transfer_ownership(*args, **kwargs):
        stored = await prepare(*args, **kwargs)
        def takeover():
            state.services.db.execute('UPDATE run_cancellations SET expires_at=? WHERE run_id=?',
                ((state.services.db.current_time()-timedelta(seconds=1)).isoformat(),state.run['id']))
            replacement.append(state.finalizer.claim(state.run['id']))
        contextvars.Context().run(takeover)
        return stored
    with monkeypatch.context() as patch:
        patch.setattr(manager,'prepare_snapshot',transfer_ownership)
        finish(state)
    assert not artifacts(state) and replacement[0]
    state.client.portal.call(state.finalizer._process,replacement[0])
    finalize_cancellation(state.services.db,state.services.events,state.run['id'])
    assert len(artifacts(state)) == 5


@pytest.mark.parametrize('field,value', [('workspace_generation',99),('run_status','SUCCEEDED'),('run_workspace',None)])
def test_replaced_workspace_or_terminal_run_invalidates_finalization(cancelled, field, value):
    state = cancelled
    fence = state.finalizer.claim(state.run['id'])
    if field == 'workspace_generation':
        state.services.db.execute('UPDATE coding_workspaces SET workspace_generation=? WHERE id=?', (value,state.bound.workspace['id']))
    elif field == 'run_workspace':
        state.services.db.execute('UPDATE runs SET coding_workspace_id=? WHERE id=?', (value,state.run['id']))
    else:
        state.services.db.execute('UPDATE runs SET status=? WHERE id=?', (value,state.run['id']))
    with execution_scope(fence), pytest.raises(LeaseLostError):
        state.services.db.assert_execution_fence()
    assert state.services.db.fetch_one('SELECT * FROM sandbox_cancellation_leases WHERE sandbox_request_id=?',
        (state.bound.sandbox_instance['id'],)) is None
    assert not artifacts(state)


def test_cancellation_during_provision_discards_only_unpublished_sandbox(runtime, tmp_path, monkeypatch):
    client, services, *_ = runtime
    run, plan = coding_run(runtime, tmp_path)
    agent = executor(services)
    fence = RunLeaseManager(services.db, 'recovery-test').claim(run['id'])
    provider = services.sandbox_manager.providers['fake']
    provision = provider.provision
    owned = []

    async def cancel_after_creation(request):
        result = await provision(request)
        owned.append(result.external_id)
        # Revoke through a separate context as a concurrent control-plane call
        # would do; the old context remains installed in the prepare operation.
        def revoke():
            services.db.execute("UPDATE runs SET status='CANCELLING' WHERE id=?", (run['id'],))
            services.db.execute('UPDATE run_attempts SET lease_token=NULL WHERE id=?', (fence.attempt_id,))
        import contextvars
        contextvars.Context().run(revoke)
        return result

    monkeypatch.setattr(provider, 'provision', cancel_after_creation)
    async def prepare():
        with execution_scope(fence):
            await agent._prepare(run, plan)
    with pytest.raises(LeaseLostError):
        client.portal.call(prepare)
    assert len(owned) == 1 and owned[0] not in provider._backends
    assert services.db.fetch_one('SELECT external_id FROM sandbox_instances WHERE id=(SELECT sandbox_instance_id FROM coding_workspaces WHERE id=?)',
        (run['coding_workspace_id'],))['external_id'] is None


@pytest.mark.asyncio
async def test_docker_provision_drains_and_cleans_up_after_task_cancellation(monkeypatch):
    import threading
    from packages.sandbox.docker_provider import DockerSandboxProvider
    from packages.sandbox.ports import SandboxProvisionRequest
    entered, finish = threading.Event(), threading.Event()
    provider = DockerSandboxProvider(image='fixture')
    monkeypatch.setattr(provider, '_ensure_image', lambda: None)
    monkeypatch.setattr(provider, '_verify_image_digest', lambda _: 'image')
    def provision(*args):
        entered.set()
        assert finish.wait(3)
        return SimpleNamespace(external_id='owned-container')
    monkeypatch.setattr(provider, '_provision_sync', provision)
    removed = []
    async def destroy(identifier):
        removed.append(identifier)
    monkeypatch.setattr(provider, 'destroy', destroy)
    request = SandboxProvisionRequest('request','tenant','project','thread','workspace',
        {'image':'fixture','network_mode':'deny_by_default'}, b'', hashlib.sha256(b'').hexdigest(), 'base')
    task = asyncio.create_task(provider.provision(request))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not removed
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == ['owned-container']


class Authority:
    row = {'workspace_id':'workspace_test','run_id':'run_test','attempt_id':'attempt_contract',
           'lease_token':'execution-token','lease_live':True,'run_status':'RUNNING'}

    def lookup(self, request_id):
        return dict(self.row) if request_id == 'sbx_service_test' else None

    def lookup_cancellation(self, request_id):
        return {**self.row, 'lease_token':'cancellation-token','finalization_status':'RUNNING'} if request_id == 'sbx_service_test' else None

    def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['expired','wrong-attempt','wrong-token','not-cancelling','completed','partition'])
async def test_cancellation_gate_stops_operations_after_authority_loss(fault):
    row = {'run_status':'CANCELLING','finalization_status':'RUNNING','attempt_id':'attempt',
           'lease_token':'cancel-token','lease_live':True}
    failed = False
    def lookup(_):
        if failed and fault == 'partition':
            raise SandboxUnavailableError('authority unavailable')
        return dict(row)
    authority = SimpleNamespace(lookup_cancellation=lookup,lookup=lambda _: None)
    stopped = []
    async def interrupt(identifier):
        stopped.append(identifier)
    gate = SandboxExecutionGate(authority,SimpleNamespace(interrupt=interrupt))
    lease = CancellationLease('attempt','cancel-token')
    assert await gate.validate('request',lease)
    with pytest.raises(LeaseLostError):
        await gate.validate('request',ExecutionLease('attempt','cancel-token'))
    state = gate.state('sandbox','request')
    state.lease = lease
    if fault == 'expired': row['lease_live'] = False
    if fault == 'wrong-attempt': row['attempt_id'] = 'different'
    if fault == 'wrong-token': row['lease_token'] = 'different'
    if fault == 'not-cancelling': row['run_status'] = 'RUNNING'
    if fault == 'completed': row['finalization_status'] = 'COMPLETED'
    failed = True
    await gate._check_one('sandbox',state)
    assert stopped == ['sandbox'] and state.lease is None


def test_remote_service_finalization_token_cannot_execute_or_write(tmp_path):
    authority = Authority()
    provider = FakeSandboxProvider(lambda command: ExecuteResponse(output='',exit_code=0))
    app = create_sandbox_service(provider=provider,state_path=str(tmp_path/'sandbox.db'),
        service_token='controller-token',image='coding:test',lease_authority=authority)
    profile = SandboxProfileSpec(provider='remote',image='coding:test',image_digest='sha256:'+'a'*64,
        memory_mb=512,disk_mb=1024).model_dump()
    source = _tar()
    auth = {'Authorization':'Bearer controller-token'}
    execution = {**auth,'X-Execution-Attempt':'attempt_contract','X-Execution-Token':'execution-token'}
    cancellation = {**auth,'X-Cancellation-Attempt':'attempt_contract','X-Cancellation-Token':'cancellation-token'}
    with TestClient(app) as client:
        response = client.post('/v1/sandboxes',headers=execution,json={
            'request_id':'sbx_service_test','scope':{'tenant_hash':'a'*64,'project_hash':'b'*64,
            'thread_hash':'c'*64,'workspace_id':'workspace_test'},'profile':profile,
            'policy_digest':hashlib.sha256(json.dumps(profile,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'source':{'content_base64':base64.b64encode(source).decode(),'sha256':hashlib.sha256(source).hexdigest(),
                      'base_commit_sha':'b'*40}})
        assert response.status_code == 201, response.text
        root = '/v1/sandboxes/' + response.json()['sandbox_id']
        assert client.post(root+'/cancel-capture',headers=cancellation).status_code == 409
        authority.row = {**authority.row, 'run_status':'CANCELLING','lease_token':None}
        assert client.post(root+'/cancel-capture',headers=execution).status_code == 409
        result = client.post(root+'/cancel-capture',headers=cancellation)
        assert result.status_code == 200, result.text
        assert result.json()['changes']['patch'] == ''
        for headers in (execution,cancellation,{**execution,'X-Execution-Token':'cancellation-token'}):
            assert client.post(root+'/execute',headers=headers,json={'command':'touch /workspace/repo/forbidden'}).status_code == 409
            assert client.post(root+'/files',headers=headers,json={'files':[]}).status_code == 409
        authority.row = {**authority.row, 'attempt_id':'new-attempt'}
        assert client.post(root+'/cancel-capture',headers=cancellation).status_code == 409


def test_inspection_rejects_truncated_or_failed_git_output():
    for exit_code, truncated in ((1,False),(0,True)):
        backend = SimpleNamespace(execute=lambda *args, **kwargs: ExecuteResponse(output='partial',exit_code=exit_code,truncated=truncated))
        with pytest.raises(SandboxUnavailableError):
            capture_changes(backend)


@pytest.mark.parametrize('fault', ['hash','path','missing','duplicate','status','shape'])
def test_capture_rejects_mismatched_or_unsafe_evidence(fault):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for root in ('workspace/repo','artifacts','tmp'):
            entry = tarfile.TarInfo(root)
            entry.type = tarfile.DIRTYPE
            tar.addfile(entry)
        entry = tarfile.TarInfo('workspace/repo/test.txt')
        entry.size = 4
        tar.addfile(entry,io.BytesIO(b'test'))
    content = output.getvalue()
    entry = {'path':'test.txt','status':'M','sha256':hashlib.sha256(b'test').hexdigest()}
    changes = {'patch':'','diff_stat':{'files':1,'added':1,'deleted':0},'changed_files':[entry]}
    if fault == 'hash':
        entry['sha256'] = '0'*64
    elif fault == 'path':
        entry['path'] = '../outside'
    elif fault == 'missing':
        entry['path'] = 'missing'
    elif fault == 'duplicate':
        changes['changed_files'].append(dict(entry))
    elif fault == 'status':
        entry['status'] = 'invented'
    else:
        changes['command'] = 'not supported'
    with pytest.raises(SandboxUnavailableError):
        validate_capture(CancellationCapture(SandboxSnapshot(content,hashlib.sha256(content).hexdigest(),len(content)),changes))


def test_schema18_upgrade_preserves_legacy_runs_without_fabricating_artifacts(runtime):
    run = new_run(runtime)
    db = runtime[1].db
    db.execute("UPDATE runs SET status='CANCELLED' WHERE id=?", (run['id'],))
    before = db.fetch_one('SELECT id,input,status,created_at FROM runs WHERE id=?', (run['id'],))
    # This schema/database belongs exclusively to the temporary runtime fixture.
    db.execute('DROP VIEW sandbox_cancellation_leases')
    db.execute('DROP TABLE run_cancellations')
    db.execute('DELETE FROM schema_migrations WHERE version=18')
    db.initialize()
    assert db.schema_versions() == list(range(1,21))
    assert db.fetch_one('SELECT id,input,status,created_at FROM runs WHERE id=?', (run['id'],)) == before
    assert not db.fetch_all('SELECT run_id FROM run_cancellations')
    assert not db.fetch_all('SELECT id FROM artifacts WHERE run_id=?', (run['id'],))
    assert not db.fetch_all('SELECT * FROM sandbox_cancellation_leases')


@pytest.mark.asyncio
async def test_remote_finalization_headers_are_distinct_and_tokens_not_sent_as_execution_leases():
    observed = []
    def handle(request):
        observed.append(request)
        return httpx.Response(409)
    provider = RemoteSandboxProvider(base_url='https://sandbox.test',service_token='service',transport=httpx.MockTransport(handle))
    with pytest.raises(LeaseLostError):
        await provider.capture_cancellation('sandbox', {})
    with execution_scope(CancellationWriteFence('run','attempt','worker','cancel-token')):
        with pytest.raises(LeaseLostError):
            await provider.capture_cancellation('sandbox', {})
    assert len(observed) == 1
    assert observed[0].headers['x-cancellation-token'] == 'cancel-token'
    assert 'x-execution-token' not in observed[0].headers
