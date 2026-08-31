from contextlib import contextmanager
from types import SimpleNamespace
import asyncio

import pytest

from packages.auth.service import AuthService, AuthenticationError, AuthAuthorizationError
from packages.domain.models import AgentCreate, AgentDraftUpdate, DeploymentCreate, RunCreate, TenantContext, ThreadCreate
from packages.evaluations.models import EvaluationRequest, EvaluationSuiteCreate
from packages.evaluations.service import EvaluationService
from packages.persistence import create_database
from packages.routing.models import RoutingProfileUpdate
from test_auth_atomicity import account
from test_atomic_review import prepare_review
from test_management_authority import revoke, assert_write_precedes_revocation
from test_runtime_concurrency import runtime


OPERATIONS = ('agent_create', 'agent_draft', 'agent_publish', 'evaluate', 'changeset', 'routing')


@pytest.fixture
def control(runtime, tmp_path):
    _, services, _, _, location = runtime
    store = SimpleNamespace(db=services.db, auth=AuthService(services.db), services=services,
                            runtime=runtime, tmp_path=tmp_path)
    store.admin = account(store, 'control.admin', super_admin=True)
    store.actor = account(store, 'control.actor', roles=['owner'])
    store.context = TenantContext(**store.actor.actor.model_dump(include={
        'tenant_id', 'project_id', 'environment_id', 'user_id', 'roles', 'session_id', 'is_super_admin'}))
    store.peer = AuthService(create_database(location))
    try:
        yield store
    finally:
        store.peer.db.close()


def operation(store, name):
    services, context = store.services, store.context
    if name == 'changeset':
        run = prepare_review(store.runtime, store.tmp_path)
        return lambda: services.coding.decide_change_set(run['id'], 'review_change', True, context, expected_version=1)
    if name == 'routing':
        services.routing.get_profile(context)
        return lambda: services.routing.update_profile(RoutingProfileUpdate(mode='shadow'), context)
    payload = AgentCreate(name='Atomic authority agent', draft={'capabilities': {'subagents': []}})
    if name == 'agent_create':
        return lambda: services.agents.create_agent(payload, context)
    agent = services.agents.create_agent(payload, context)
    if name == 'agent_draft':
        update = AgentDraftUpdate(version=agent['version'], draft=agent['draft'], name='Updated authority agent')
        return lambda: services.agents.update_draft(agent['id'], update, context)
    if name == 'agent_publish':
        return lambda: services.agents.publish(agent['id'], context)
    revision = services.agents.publish(agent['id'], context)['revision']
    deployment = services.agents.deploy(DeploymentCreate(agent_revision_id=revision['id']), context)
    evaluations = EvaluationService(store.db)
    suite = evaluations.create_suite(EvaluationSuiteCreate(name='Control authority suite', cases=[{
        'id': 'one', 'category': 'functional', 'input': 'Fixture input', 'output_contains': ['fixture']}]), context)
    thread = services.runs.create_thread(ThreadCreate(agent_deployment_id=deployment['id']), context)
    run = asyncio.run(services.runs.create_run(thread['id'], RunCreate(input='Fixture input'), context))
    # This fixture only exercises publication authority. It has no real provider
    # evidence and must never obtain production eligibility.
    store.db.execute("UPDATE runs SET status='SUCCEEDED',output='fixture' WHERE id=?", (run['id'],))
    request = EvaluationRequest(suite_id=suite['id'], case_runs={'one': run['id']})
    def evaluate():
        result = evaluations.evaluate(revision['id'], request, context, 'authority-evaluation')
        assert not result['production_eligible']
        return result
    return evaluate


def snapshot(db):
    tables = {'agents': 'id', 'agent_revisions': 'id', 'resolved_execution_plans': 'id',
        'evaluation_results': 'id', 'idempotency_records': 'tenant_id,scope,key', 'change_sets': 'id',
        'run_events': 'event_id', 'intent_router_revisions': 'id', 'release_projects': 'tenant_id,project_id',
        'governance_audit_events': 'id'}
    return {table: db.fetch_all(f'SELECT * FROM {table} ORDER BY {order}') for table, order in tables.items()}


@pytest.mark.parametrize('name', OPERATIONS)
def test_control_plane_rechecks_revocation_before_publishing(control, monkeypatch, name):
    invoke = operation(control, name)
    before = snapshot(control.db)
    original = control.db.transaction
    reached = []

    @contextmanager
    def revoke_before_transaction():
        if not reached:
            reached.append(True)
            revoke(control.peer, control, control.peer.get_user(control.context.user_id))
        with original() as connection:
            yield connection

    monkeypatch.setattr(control.db, 'transaction', revoke_before_transaction)
    with pytest.raises((AuthenticationError, AuthAuthorizationError)):
        invoke()
    assert reached
    assert snapshot(control.db) == before


@pytest.mark.parametrize('name', OPERATIONS)
def test_control_plane_valid_authority_still_completes(control, name):
    assert operation(control, name)()


@pytest.mark.parametrize('name', OPERATIONS)
def test_control_plane_write_and_audit_precede_concurrent_revocation(control, monkeypatch, name):
    invoke = operation(control, name)
    assert_write_precedes_revocation(control, invoke, control.peer, monkeypatch,
        audit_table='run_events' if name == 'changeset' else 'governance_audit_events')


@pytest.mark.parametrize('name', ['agent_create', 'agent_draft', 'agent_publish', 'evaluate', 'routing'])
def test_control_plane_audit_failure_rolls_back_published_state(control, monkeypatch, name):
    invoke = operation(control, name)
    before = snapshot(control.db)
    original = control.db.execute

    def fail_after_audit(sql, params=()):
        result = original(sql, params)
        if 'INSERT INTO governance_audit_events' in sql:
            raise RuntimeError('Synthetic control-plane audit failure')
        return result

    monkeypatch.setattr(control.db, 'execute', fail_after_audit)
    with pytest.raises(RuntimeError, match='Synthetic control-plane audit failure'):
        invoke()
    assert snapshot(control.db) == before


def test_revocation_during_compilation_does_not_publish_revision(control, monkeypatch):
    invoke = operation(control, 'agent_publish')
    before = snapshot(control.db)
    compiler = control.services.agents.compiler
    original = compiler.compile

    def compile_then_revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        revoke(control.peer, control, control.peer.get_user(control.context.user_id))
        return result

    monkeypatch.setattr(compiler, 'compile', compile_then_revoke)
    with pytest.raises((AuthenticationError, AuthAuthorizationError)):
        invoke()
    assert snapshot(control.db) == before
