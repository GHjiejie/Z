"""Real account revocation interleavings, using only disposable databases."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta
from threading import Event

import pytest

from packages.auth.models import UserDeleteRequest, UserUpdate
from packages.auth.service import AuthenticationError, AuthAuthorizationError
from packages.billing.models import PricePolicy, QuotaPolicy, Reconciliation
from packages.billing.service import BillingService
from packages.domain.models import TenantContext
from packages.evaluations.models import EvaluationPolicyUpdate, EvaluationSuiteCreate
from packages.evaluations.service import EvaluationService
from packages.runtime.model_gateway import DeterministicModelGateway
from packages.runtime.model_registry import ModelProfile, ModelRegistration, ModelRegistry, ModelStatusUpdate
from test_auth_atomicity import auth_store, account


OPERATIONS = ('price', 'quota', 'reconcile', 'model_register', 'model_status', 'suite', 'policy')
REASON = 'Isolated management authority regression'


@pytest.fixture
def management(auth_store):
    store = auth_store
    store.admin = account(store, 'governance.admin', super_admin=True)
    store.actor = account(store, 'governance.actor', roles=['owner'])
    actor = store.actor.actor
    store.context = TenantContext(**actor.model_dump(include={
        'tenant_id', 'project_id', 'environment_id', 'user_id', 'roles', 'session_id', 'is_super_admin'}))
    return store


def suite_payload():
    return EvaluationSuiteCreate(name='Authority fixture', cases=[
        {'id': 'functional', 'category': 'functional', 'input': 'fixture', 'output_contains': ['fixture']},
        {'id': 'safety', 'category': 'safety', 'input': 'fixture', 'expected_status': 'WAITING_FOR_APPROVAL'},
        {'id': 'recovery', 'category': 'recovery', 'input': 'fixture'},
        {'id': 'cost', 'category': 'cost', 'input': 'fixture', 'max_cost': 1},
    ])


def operation(store, name):
    db, context = store.db, store.context
    billing = BillingService(db)
    identity = {'provider': 'synthetic', 'route': 'fixture', 'model': 'fixture'}
    if name == 'price':
        payload = PricePolicy(version=0, reason=REASON, identity=identity, input_per_million=1, output_per_million=2)
        return lambda: billing.update_price(payload, context)
    if name == 'quota':
        payload = QuotaPolicy(version=0, reason=REASON, scope_type='project', subject_id=context.project_id, max_calls=10)
        return lambda: billing.update_quota(payload, context)
    if name == 'reconcile':
        ticket = billing.meter.reserve(context, identity, {'input_per_million': 1, 'output_per_million': 2},
            purpose='intent_classification', resource_id='fixture', input_tokens=3, output_tokens=2)
        db.execute("UPDATE metered_calls SET billing_status='UNCERTAIN',active_until=NULL WHERE id=?", (ticket.call_id,))
        payload = Reconciliation(version=1, reason=REASON, input_tokens=1, output_tokens=1,
            actual_cost_micro_usd=3, provider_receipt='synthetic-receipt')
        return lambda: billing.reconcile(ticket.call_id, payload, context)
    if name.startswith('model_'):
        registry = ModelRegistry(db, DeterministicModelGateway())
        registry.profiles = {'fixture': ModelProfile(id='fixture', name='Fixture',
            tenant_id=context.tenant_id, project_id=context.project_id, model='fixture',
            base_url='https://approved.test/v1', credential_env='DEEPAGENT_MODEL_KEY_FIXTURE',
            input_per_million=1, output_per_million=2)}
        payload = ModelRegistration(profile_id='fixture', reason=REASON)
        if name == 'model_register':
            return lambda: registry.register(payload, context)
        registered = registry.register(payload, context)
        status = ModelStatusUpdate(version=1, enabled=False, reason=REASON)
        return lambda: registry.update_status(registered['id'], status, context)
    evaluations = EvaluationService(db)
    if name == 'suite':
        return lambda: evaluations.create_suite(suite_payload(), context)
    suite = evaluations.create_suite(suite_payload(), context)
    payload = EvaluationPolicyUpdate(suite_id=suite['id'], version=0, reason=REASON)
    return lambda: evaluations.update_policy(payload, context)


def snapshot(db):
    tables = {'billing_price_policies': 'tenant_id,project_id,model_key',
        'billing_quota_policies': 'tenant_id,scope_type,subject_id,period', 'metered_calls': 'id',
        'model_deployments': 'id', 'evaluation_suites': 'id', 'evaluation_policies': 'tenant_id,project_id',
        'governance_audit_events': 'id'}
    return {table: db.fetch_all(f'SELECT * FROM {table} ORDER BY {order}') for table, order in tables.items()}


@pytest.mark.parametrize('name', OPERATIONS)
@pytest.mark.parametrize('revocation', ['roles', 'session', 'disabled', 'scope'])
def test_revocation_committed_between_precheck_and_write_rejects_all_changes(management, monkeypatch, name, revocation):
    store = management
    invoke = operation(store, name)
    before = snapshot(store.db)
    peer = store.peer()
    original = store.db.transaction
    reached = []

    @contextmanager
    def revoke_before_transaction():
        if not reached:
            reached.append(True)
            current = peer.get_user(store.actor.user['id'])
            revoke(peer, store, current, revocation)
        with original() as connection:
            yield connection

    monkeypatch.setattr(store.db, 'transaction', revoke_before_transaction)
    with pytest.raises((AuthenticationError, AuthAuthorizationError)):
        invoke()
    assert reached
    assert snapshot(store.db) == before


def revoke(peer, store, current, kind='roles'):
    if kind == 'session':
        peer.revoke_managed_session(current['id'], store.context.session_id, store.admin.actor)
    elif kind == 'disabled':
        peer.deactivate_user(current['id'], UserDeleteRequest(version=current['version'], reason=REASON), store.admin.actor)
    else:
        change = {'roles': ['member']} if kind == 'roles' else {'project_id': 'revoked-project'}
        peer.update_user(current['id'], UserUpdate(version=current['version'], **change), store.admin.actor)


@pytest.mark.parametrize('name', OPERATIONS)
def test_authorized_management_change_and_audit_commit_together(management, name):
    invoke = operation(management, name)
    before = management.db.fetch_one('SELECT COUNT(*) AS n FROM governance_audit_events')['n']
    invoke()
    assert management.db.fetch_one('SELECT COUNT(*) AS n FROM governance_audit_events')['n'] == before + 1


@pytest.mark.parametrize('name', OPERATIONS)
@pytest.mark.parametrize('after_write', [False, True])
def test_management_audit_failure_rolls_back_every_business_change(management, monkeypatch, name, after_write):
    invoke = operation(management, name)
    before = snapshot(management.db)
    original = management.db.execute

    def fail(sql, params=()):
        targeted = 'INSERT INTO governance_audit_events' in sql
        if targeted and not after_write:
            raise RuntimeError('Synthetic audit failure')
        result = original(sql, params)
        if targeted:
            raise RuntimeError('Synthetic audit failure')
        return result

    monkeypatch.setattr(management.db, 'execute', fail)
    with pytest.raises(RuntimeError, match='Synthetic audit failure'):
        invoke()
    assert snapshot(management.db) == before


@pytest.mark.parametrize('name', OPERATIONS)
def test_management_write_holds_authority_until_its_audit_commits(management, monkeypatch, name):
    store = management
    invoke = operation(store, name)
    peer = store.peer()
    assert_write_precedes_revocation(store, invoke, peer, monkeypatch)


def assert_write_precedes_revocation(store, invoke, peer, monkeypatch, *, audit_table='governance_audit_events'):
    current = peer.get_user(store.actor.user['id'])
    written, release, revoking = Event(), Event(), Event()
    original_write, original_transaction = store.db.execute, peer.db.transaction

    def pause_after_audit(sql, params=()):
        result = original_write(sql, params)
        if f'INSERT INTO {audit_table}' in sql:
            written.set()
            assert release.wait(10), 'Fixture must release the owned write'
        return result

    @contextmanager
    def revocation_transaction():
        revoking.set()
        with original_transaction() as connection:
            yield connection

    monkeypatch.setattr(store.db, 'execute', pause_after_audit)
    monkeypatch.setattr(peer.db, 'transaction', revocation_transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(invoke)
        try:
            assert written.wait(10)
            revoker = pool.submit(revoke, peer, store, current)
            assert revoking.wait(10)
            with pytest.raises(FutureTimeout):
                revoker.result(timeout=.15)
        finally:
            release.set()
        writer.result(timeout=10)
        revoker.result(timeout=10)
    assert peer.get_user(current['id'])['roles'] == ['member']
    with pytest.raises((AuthenticationError, AuthAuthorizationError)):
        invoke()


def test_reciprocal_user_quota_updates_use_sorted_account_locks(management):
    store = management
    other = account(store, 'governance.other', roles=['owner'])
    contexts = [store.context, TenantContext(**other.actor.model_dump(include={
        'tenant_id', 'project_id', 'environment_id', 'user_id', 'roles', 'session_id', 'is_super_admin'}))]
    peers = [BillingService(store.peer().db) for _ in contexts]
    def update(index):
        return peers[index].update_quota(QuotaPolicy(version=0, reason=REASON, scope_type='user',
            subject_id=contexts[1-index].user_id, max_calls=10), contexts[index])
    from test_runtime_concurrency import race
    results = race(update, count=2)
    assert [row['version'] for row in results] == [1, 1]
    assert {row['subject_id'] for row in results} == {context.user_id for context in contexts}


def test_user_quota_rechecks_target_scope_inside_transaction(management, monkeypatch):
    store = management
    target = account(store, 'governance.target')
    peer = store.peer()
    payload = QuotaPolicy(version=0, reason=REASON, scope_type='user', subject_id=target.user['id'], max_calls=10)
    before, original, changed = snapshot(store.db), store.db.transaction, []
    @contextmanager
    def move_target_before_write():
        if not changed:
            changed.append(True)
            peer.update_user(target.user['id'], UserUpdate(version=target.user['version'], project_id='other-project'), store.admin.actor)
        with original() as connection:
            yield connection
    monkeypatch.setattr(store.db, 'transaction', move_target_before_write)
    from packages.application.services import NotFoundError
    with pytest.raises(NotFoundError):
        BillingService(store.db).update_quota(payload, store.context)
    assert snapshot(store.db) == before


@pytest.mark.parametrize('name', OPERATIONS)
def test_session_expiry_during_write_rolls_back_business_and_audit(management, monkeypatch, name):
    invoke = operation(management, name)
    before = snapshot(management.db)
    original = management.db.execute
    expired_time = management.db.current_time() + timedelta(days=365)

    def expire_after_audit(sql, params=()):
        result = original(sql, params)
        if 'INSERT INTO governance_audit_events' in sql:
            monkeypatch.setattr(management.db, 'current_time', lambda: expired_time)
        return result

    monkeypatch.setattr(management.db, 'execute', expire_after_audit)
    with pytest.raises(AuthenticationError):
        invoke()
    assert snapshot(management.db) == before


@pytest.mark.parametrize('policy', ['forced', 'expired'])
def test_interactive_management_rejects_password_policy_changes(management, policy):
    invoke = operation(management, 'price')
    if policy == 'forced':
        management.db.execute('UPDATE users SET must_change_password=1 WHERE id=?', (management.context.user_id,))
    else:
        management.db.execute("UPDATE users SET password_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (management.context.user_id,))
    before = snapshot(management.db)
    with pytest.raises(AuthAuthorizationError, match='Password change'):
        invoke()
    assert snapshot(management.db) == before
