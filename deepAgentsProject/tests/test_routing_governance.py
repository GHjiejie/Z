from datetime import timedelta

import pytest

from release_helpers import user_headers
from test_releases import release_runtime, request, decide
from test_runtime_concurrency import runtime, race


@pytest.fixture
def governed(release_runtime):
    client, services, revision, suite, requester, reviewer, admin = release_runtime
    release = decide(client, request(client, revision, requester), reviewer).json()
    profile = client.get('/api/v1/production-routing/profile', headers=requester)
    assert profile.status_code == 200, profile.text
    yield client, services, revision, requester, reviewer, admin, profile.json()['profile'], release


def create(client, actor, base_profile, *, key=None, **changes):
    body = {'expected_router_revision_id': base_profile['id'], 'reason': 'Reviewed production routing change',
        'profile': {'mode': 'disabled', **base_profile['config']}, **changes}
    return client.post('/api/v1/routing-change-requests', headers={**actor, **({'Idempotency-Key': key} if key else {})}, json=body)


def approve(client, actor, item, **changes):
    return client.post(f"/api/v1/routing-change-requests/{item['id']}:decide", headers=actor,
        json={'version': item['version'], 'decision': 'approve', 'reason': 'Independent production routing review', **changes})


def test_production_put_blocked_and_legacy_not_inferred_approved(governed):
    client, services, _, requester, reviewer, _, profile, release = governed
    production = user_headers(services, 'routing_owner', 'owner', 'env_production')
    assert client.put('/api/v1/intent-routing/profile', headers=production, json={'mode': 'disabled'}).status_code == 409
    assert profile['approval_state'] == 'LEGACY'
    assert client.post('/api/v1/intent-routing:resolve', headers=production, json={'input': 'hello'}).status_code == 409
    assert create(client, production, profile).status_code == 403
    assert client.get('/api/v1/production-routing/profile', headers=production).status_code == 403
    response = create(client, requester, profile)
    assert response.status_code == 202, response.text
    item = response.json()
    assert client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']['id'] == profile['id']
    assert approve(client, requester, item).status_code == 403
    result = approve(client, reviewer, item)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'APPLIED'
    active = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    assert active['approval_state'] == 'APPROVED' and active['id'] != profile['id']
    resolved = client.post('/api/v1/intent-routing:resolve', headers=production, json={'input': 'hello'})
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()['selected_deployment_id'] == release['deployment_id']
    assert 'requester_context' not in item and 'snapshot_json' not in item


def test_idempotency_and_concurrent_decision_apply_exactly_once(governed):
    client, services, _, requester, reviewer, _, profile, _ = governed
    first = create(client, requester, profile, key='one-change')
    assert first.status_code == 202, first.text
    item = first.json()
    assert create(client, requester, profile, key='one-change').json()['id'] == item['id']
    assert create(client, requester, profile, key='one-change', reason='Different reason for this change').status_code == 409
    responses = race(lambda _: approve(client, reviewer, item), count=4)
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()['router_revision_id'] for response in responses}) == 1
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM governance_audit_events WHERE action='routing.update.applied'")['n'] == 1
    assert approve(client, reviewer, item, reason='Changed decision content').status_code == 409
    assert approve(client, reviewer, item, decision='reject').status_code == 409


@pytest.mark.parametrize('change,expected', [
    ('requester_grant', 403), ('reviewer_grant', 403), ('reissued', 409), ('role', 403),
    ('disabled', 403), ('expired', 409), ('snapshot', 409), ('model', 409), ('evaluation', 409), ('route', 409),
])
def test_pending_changes_revalidate_every_boundary(governed, monkeypatch, change, expected):
    client, services, revision, requester, reviewer, admin, profile, _ = governed
    response = create(client, requester, profile)
    assert response.status_code == 202, response.text
    item = response.json()
    if change in {'requester_grant', 'reviewer_grant', 'reissued'}:
        actor = reviewer if change == 'reviewer_grant' else requester
        body = {'user_id': actor['X-User-ID'], 'environment': 'production', 'version': 1,
            'can_deploy': False, 'can_approve': False, 'reason': 'Revoke production authority'}
        assert client.put('/api/v1/deployment-environment-grants', headers=admin, json=body).status_code == 200
        if change == 'reissued':
            assert client.put('/api/v1/deployment-environment-grants', headers=admin,
                json={**body, 'version': 2, 'can_deploy': True, 'can_approve': True}).status_code == 200
    elif change == 'role':
        services.db.execute("UPDATE users SET roles_json='[\"member\"]' WHERE id=?", (requester['X-User-ID'],))
    elif change == 'disabled':
        services.db.execute("UPDATE users SET status='INACTIVE' WHERE id=?", (requester['X-User-ID'],))
    elif change == 'expired':
        clock = services.db.current_time
        monkeypatch.setattr(services.db, 'current_time', lambda: clock() + timedelta(seconds=3601))
    elif change == 'snapshot':
        services.db.execute("UPDATE routing_change_requests SET snapshot_json='{}' WHERE id=?", (item['id'],))
    elif change == 'model':
        plan = services.db.fetch_one('SELECT * FROM resolved_execution_plans WHERE agent_revision_id=?', (revision['id'],))['plan']
        services.db.execute("UPDATE model_deployments SET status='disabled' WHERE id=?", (plan['model_deployment_revision_id'],))
    elif change == 'evaluation':
        services.db.execute('UPDATE evaluation_policies SET version=version+1')
    elif change == 'route':
        other = create(client, requester, profile, reason='Different pending routing change').json()
        assert approve(client, reviewer, other).status_code == 200
    result = approve(client, reviewer, item)
    assert result.status_code == expected, result.text
    assert services.db.fetch_one('SELECT status FROM routing_change_requests WHERE id=?', (item['id'],))['status'] == 'PENDING'


def test_deployment_change_invalidates_pending_route_and_propagates_approval(governed):
    client, _, revision, requester, reviewer, _, profile, _ = governed
    first = approve(client, reviewer, create(client, requester, profile).json())
    assert first.status_code == 200, first.text
    current = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    pending = create(client, requester, current).json()
    promoted = decide(client, request(client, revision, requester, version=1), reviewer)
    assert promoted.status_code == 200, promoted.text
    assert approve(client, reviewer, pending).status_code == 409
    updated = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    assert updated['approval_state'] == 'APPROVED'
    assert updated['config']['target_deployments']['general'] == promoted.json()['deployment_id']


def test_rollback_is_a_new_review_and_does_not_resurrect_drained_targets(governed):
    client, _, revision, requester, reviewer, _, profile, _ = governed
    approved = approve(client, reviewer, create(client, requester, profile).json()).json()
    first = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    changed = create(client, requester, first, profile={'mode': 'shadow', **first['config']}).json()
    assert approve(client, reviewer, changed).status_code == 200
    second = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    rollback = create(client, requester, second, action='rollback', profile=None, rollback_revision_id=approved['router_revision_id'])
    assert rollback.status_code == 202, rollback.text
    assert approve(client, reviewer, rollback.json()).status_code == 200
    restored = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    assert restored['mode'] == 'disabled' and restored['id'] != first['id']
    assert decide(client, request(client, revision, requester, version=1), reviewer).status_code == 200
    latest = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    assert create(client, requester, latest, action='rollback', profile=None, rollback_revision_id=first['id']).status_code == 404


def test_private_requests_pagination_cancel_and_audit_rollback(governed, monkeypatch):
    client, services, _, requester, reviewer, _, profile, _ = governed
    outsider = user_headers(services, 'routing_outsider')
    item = create(client, requester, profile).json()
    assert client.get(f"/api/v1/routing-change-requests/{item['id']}", headers=outsider).status_code == 404
    assert client.get('/api/v1/routing-change-requests', headers=outsider).json()['items'] == []
    original = services.db.execute
    def fail_audit(sql, params=()):
        if 'INSERT INTO governance_audit_events' in sql and 'routing.update.applied' in params:
            raise RuntimeError('Injected routing audit failure')
        return original(sql, params)
    monkeypatch.setattr(services.db, 'execute', fail_audit)
    with pytest.raises(RuntimeError, match='Injected'):
        approve(client, reviewer, item)
    assert client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']['id'] == profile['id']
    monkeypatch.setattr(services.db, 'execute', original)
    assert client.post(f"/api/v1/routing-change-requests/{item['id']}:cancel", headers=reviewer,
        json={'version': 1, 'reason': 'Cancel another user request'}).status_code == 403
    cancellation = {'version': 1, 'reason': 'Cancel my pending routing change'}
    for _ in range(2):
        assert client.post(f"/api/v1/routing-change-requests/{item['id']}:cancel", headers=requester, json=cancellation).status_code == 200
    assert approve(client, reviewer, item).status_code == 409
    for _ in range(2):
        assert create(client, requester, profile).status_code == 202
    page = client.get('/api/v1/routing-change-requests?limit=1', headers=reviewer).json()
    assert len(page['items']) == 1 and page['has_more']
    assert client.get('/api/v1/routing-change-requests', headers=requester, params={'cursor': page['next_cursor']}).status_code == 400


def test_old_decision_rejected_and_unconfigured_targets_never_discovered(governed):
    client, services, _, requester, reviewer, _, profile, _ = governed
    assert approve(client, reviewer, create(client, requester, profile).json()).status_code == 200
    production = user_headers(services, 'stale_route_member', 'member', 'env_production')
    response = client.post('/api/v1/intent-routing:resolve', headers=production, json={'input': 'hello'})
    assert response.status_code == 201, response.text
    decision = response.json()
    current = client.get('/api/v1/production-routing/profile', headers=requester).json()['profile']
    empty = {'mode': 'disabled', 'target_deployments': {intent: None for intent in ('general', 'release', 'knowledge', 'coding')}}
    assert approve(client, reviewer, create(client, requester, current, profile=empty).json()).status_code == 200
    assert client.post('/api/v1/routed-runs', headers=production, json={
        'decision_id': decision['id'], 'input': 'hello', 'confirmed': True}).status_code == 409
    assert client.post('/api/v1/intent-routing:resolve', headers=production, json={'input': 'hello'}).status_code == 409


def test_scope_validation_and_candidate_validation(governed):
    client, services, _, requester, reviewer, _, profile, release = governed
    item = create(client, requester, profile).json()
    other = user_headers(services, 'other_project_routing')
    services.db.execute("UPDATE users SET project_id='other_project' WHERE id=?", (other['X-User-ID'],))
    other['X-Project-ID'] = 'other_project'
    assert client.get(f"/api/v1/routing-change-requests/{item['id']}", headers=other).status_code == 404
    assert client.get('/api/v1/routing-change-requests', headers=other).json()['items'] == []
    assert approve(client, other, item).status_code == 404
    development = client.get('/api/v1/agent-deployments', headers=requester).json()['items'][0]
    assert create(client, requester, profile, profile={'target_deployments': {'general': development['id']}}).status_code == 404
    assert create(client, requester, profile, profile={'target_deployments': {'coding': release['deployment_id']}}).status_code == 409
    assert create(client, requester, profile, action='rollback', profile=None, rollback_revision_id=profile['id']).status_code == 404
    assert create(client, requester, profile, reason='     ').status_code == 422
    assert create(client, requester, profile, profile={'unknown_field': True}).status_code == 422


def test_cancel_still_available_after_grant_revocation_and_reject_does_not_apply(governed):
    client, services, _, requester, reviewer, admin, profile, _ = governed
    item = create(client, requester, profile).json()
    rejected = create(client, requester, profile).json()
    assert approve(client, reviewer, rejected, decision='reject').status_code == 200
    assert client.put('/api/v1/deployment-environment-grants', headers=admin, json={
        'user_id': requester['X-User-ID'], 'environment': 'production', 'version': 1,
        'can_deploy': False, 'can_approve': False, 'reason': 'Revoke pending requester authority'}).status_code == 200
    assert client.post(f"/api/v1/routing-change-requests/{item['id']}:cancel", headers=requester,
        json={'version': 1, 'reason': 'Cancel after my authority was revoked'}).status_code == 200
    current = services.db.fetch_one("SELECT id FROM intent_router_revisions WHERE environment_id='env_production' AND status='ACTIVE'")
    assert current['id'] == profile['id']


def test_release_and_route_review_compete_for_one_production_state(governed):
    client, services, revision, requester, reviewer, _, profile, _ = governed
    routing = create(client, requester, profile).json()
    promotion = request(client, revision, requester, version=1)
    outcomes = race(lambda index: approve(client, reviewer, routing) if index == 0 else decide(client, promotion, reviewer), count=2)
    assert sorted(result.status_code for result in outcomes) == [200, 409]
    assert services.db.fetch_one("SELECT COUNT(*) AS n FROM intent_router_revisions WHERE environment_id='env_production' AND status='ACTIVE'")['n'] == 1


def test_schema_17_preserves_legacy_configuration_without_inventing_approval(tmp_path, monkeypatch):
    from packages.persistence import Database
    db = Database(str(tmp_path / 'routing-migration.db'))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Database, '_migration_production_routing', lambda self: None)
            db.initialize()
        db.execute('DELETE FROM schema_migrations WHERE version=17')
        db.execute("""INSERT INTO intent_router_revisions
            (id,tenant_id,project_id,environment_id,revision_number,taxonomy_version,mode,config_json,model_snapshot_json,status,created_at)
            VALUES('legacy','tenant','project','env_production',1,'1.0','disabled','{}','{}','ACTIVE','2026-01-01T00:00:00+00:00')""")
        before = db.fetch_one("SELECT * FROM intent_router_revisions WHERE id='legacy'")
        db.initialize()
        after = db.fetch_one("SELECT * FROM intent_router_revisions WHERE id='legacy'")
        assert after.pop('approval_state') == 'LEGACY'
        assert after == before
        db.initialize()
        assert db.fetch_one('SELECT COUNT(*) AS n FROM routing_change_requests')['n'] == 0
        assert db.schema_versions() == list(range(1, 21))
    finally:
        db.close()
