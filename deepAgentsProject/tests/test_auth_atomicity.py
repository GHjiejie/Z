from __future__ import annotations

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from pydantic import ValidationError

from packages.auth.models import PasswordChangeRequest, PasswordResetRequest, UserCreate, UserDeleteRequest, UserUpdate
from packages.auth.service import AuthService, AuthenticationError, AuthAuthorizationError, AuthConflictError, AuthRateLimitError, AuthValidationError
from packages.persistence import create_database


PASSWORD = "AtomicFixture1!"
NEW_PASSWORD = "AtomicChanged2!"


@pytest.fixture(params=["sqlite", "postgresql"])
def auth_store(request, tmp_path):
    location = str(tmp_path / "auth-atomic.db")
    admin, schema = None, None
    databases = []
    try:
        if request.param == "postgresql":
            url = os.getenv("DEEPAGENT_TEST_POSTGRES_URL")
            if not url:
                pytest.skip("DEEPAGENT_TEST_POSTGRES_URL is required")
            import psycopg
            from psycopg import sql
            admin = psycopg.connect(url, autocommit=True)
            admin.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
            schema = "auth_atomic_" + secrets.token_hex(10)
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            query["options"] = f"-csearch_path={schema},public"
            location = urlunsplit(parts._replace(query=urlencode(query)))
        db = create_database(location)
        databases.append(db)
        db.initialize()

        def peer():
            other = create_database(location)
            databases.append(other)
            return AuthService(other)

        yield SimpleNamespace(db=db, auth=AuthService(db), peer=peer)
    finally:
        for database in reversed(databases):
            database.close()
        if admin:
            if schema:
                from psycopg import sql
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()


def account(store, name, *, super_admin=False, roles=None):
    user = store.auth.create_user(UserCreate(username=name, display_name=name,
        password=PASSWORD, is_super_admin=super_admin, roles=roles or ["member"]))
    store.db.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user["id"],))
    session = store.auth.login(name, PASSWORD)
    return SimpleNamespace(user=store.auth.get_user(user["id"]), token=session["access_token"],
                           actor=store.auth.authenticate(session["access_token"]))


@pytest.fixture
def accounts(auth_store):
    auth_store.admin = account(auth_store, "admin", super_admin=True)
    auth_store.member = account(auth_store, "atomic.member")
    session = auth_store.auth.login("atomic.member", PASSWORD)
    auth_store.other_token = session["access_token"]
    auth_store.other_session = auth_store.auth.authenticate(session["access_token"]).session_id
    return auth_store


def snapshot(store):
    # Include secrets only in local equality checks; never print them in output.
    return {table: store.db.fetch_all(f"SELECT * FROM {table} ORDER BY {key}")
            for table, key in (("users", "id"), ("auth_sessions", "id"),
                               ("auth_audit_events", "id"), ("auth_login_limits", "key_hash"))}


def mutation(store, operation):
    auth, member, admin = store.auth, store.member, store.admin.actor
    user_id, version = member.user["id"], member.user["version"]
    return {
        "create": lambda: auth.create_user(UserCreate(username="atomic.new", display_name="New", password=PASSWORD), admin),
        "update": lambda: auth.update_user(user_id, UserUpdate(version=version, roles=["viewer"]), admin),
        "reset": lambda: auth.reset_password(user_id, PasswordResetRequest(password=NEW_PASSWORD, version=version), admin),
        "change": lambda: auth.change_own_password(PasswordChangeRequest(current_password=PASSWORD,
            new_password=NEW_PASSWORD, version=version), member.actor),
        "deactivate": lambda: auth.deactivate_user(user_id, UserDeleteRequest(version=version, reason="Fixture account departure"), admin),
        "revoke_one": lambda: auth.revoke_managed_session(user_id, store.other_session, admin),
        "revoke_all": lambda: auth.revoke_all_managed_sessions(user_id, admin),
        "logout": lambda: auth.logout(member.actor),
        "bootstrap": lambda: auth.bootstrap_super_admin(PASSWORD),
        "login": lambda: auth.login("atomic.member", PASSWORD),
    }[operation]


@pytest.mark.parametrize("operation", ["create", "update", "reset", "change", "deactivate",
                                       "revoke_one", "revoke_all", "logout", "bootstrap", "login"])
@pytest.mark.parametrize("after_write", [False, True])
def test_audit_failure_rolls_back_every_account_side_effect(accounts, monkeypatch, operation, after_write):
    before = snapshot(accounts)
    original = accounts.auth._audit

    def failed(*args, **kwargs):
        if after_write:
            original(*args, **kwargs)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(accounts.auth, "_audit", failed)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        mutation(accounts, operation)()
    assert snapshot(accounts) == before
    monkeypatch.setattr(accounts.auth, "_audit", original)
    mutation(accounts, operation)()  # The original optimistic version still works.


@pytest.mark.parametrize("operation", ["update", "reset", "change", "deactivate", "revoke_all"])
def test_partial_session_revocation_also_rolls_back(accounts, monkeypatch, operation):
    before = snapshot(accounts)
    original = accounts.auth.revoke_user_sessions

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected revocation failure")

    monkeypatch.setattr(accounts.auth, "revoke_user_sessions", failed)
    with pytest.raises(RuntimeError, match="injected revocation failure"):
        mutation(accounts, operation)()
    assert snapshot(accounts) == before
    monkeypatch.setattr(accounts.auth, "revoke_user_sessions", original)
    mutation(accounts, operation)()
    with pytest.raises(AuthenticationError):
        accounts.auth.authenticate(accounts.other_token)


def test_bootstrap_create_and_repair_are_atomic_and_idempotent(auth_store, monkeypatch):
    original = auth_store.auth._audit

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(auth_store.auth, "_audit", failed)
    with pytest.raises(RuntimeError, match="injected bootstrap failure"):
        auth_store.auth.bootstrap_super_admin(PASSWORD)
    assert not auth_store.db.fetch_all("SELECT id FROM users")
    assert not auth_store.db.fetch_all("SELECT id FROM auth_audit_events")
    monkeypatch.setattr(auth_store.auth, "_audit", original)
    first = auth_store.auth.bootstrap_super_admin(PASSWORD)
    before = snapshot(auth_store)
    assert auth_store.auth.bootstrap_super_admin(PASSWORD) == first
    assert snapshot(auth_store) == before


@pytest.mark.parametrize("change", [{"username": "renamed.member"}, {"tenant_id": "tenant_new"},
    {"project_id": "project_new"}, {"environment_id": "env_production"},
    {"roles": ["reviewer"]}, {"is_super_admin": True}])
def test_security_changes_revoke_old_tokens_but_cosmetic_edits_do_not(accounts, change):
    member = accounts.member
    auth = accounts.auth
    changed = auth.update_user(member.user["id"], UserUpdate(version=1, display_name="New display"), accounts.admin.actor)
    assert auth.authenticate(member.token).display_name == "New display"
    changed = auth.update_user(member.user["id"], UserUpdate(version=changed["version"], **change), accounts.admin.actor)
    for token in (member.token, accounts.other_token):
        with pytest.raises(AuthenticationError):
            auth.authenticate(token)
    login = auth.login(changed["username"], PASSWORD)
    assert auth.authenticate(login["access_token"]).user_id == member.user["id"]


@pytest.mark.parametrize("field", ["username", "display_name", "tenant_id", "project_id",
                                   "environment_id", "roles", "is_super_admin", "status"])
def test_patch_null_is_rejected_before_database_writes(field):
    with pytest.raises(ValidationError, match="cannot be null"):
        UserUpdate(version=1, **{field: None})


def test_duplicate_username_is_a_domain_conflict_and_does_not_poison_transaction(accounts):
    before = snapshot(accounts)
    with pytest.raises(AuthConflictError, match="Username already exists"):
        accounts.auth.create_user(UserCreate(username="ATOMIC.MEMBER", display_name="Duplicate", password=PASSWORD), accounts.admin.actor)
    with pytest.raises(AuthConflictError, match="Username already exists"):
        accounts.auth.update_user(accounts.member.user["id"], UserUpdate(version=1, username="ADMIN"), accounts.admin.actor)
    assert snapshot(accounts) == before
    mutation(accounts, "update")()


def test_denied_login_and_password_change_commit_only_denial_evidence(accounts):
    before = snapshot(accounts)
    with pytest.raises(AuthenticationError):
        accounts.auth.login("atomic.member", "not-the-password")
    user = accounts.db.fetch_one("SELECT * FROM users WHERE id=?", (accounts.member.user["id"],))
    assert user["failed_login_count"] == 1
    assert accounts.db.fetch_one("SELECT COUNT(*) AS count FROM auth_login_limits")["count"] == 1
    assert accounts.db.fetch_all("SELECT * FROM auth_sessions ORDER BY id") == before["auth_sessions"]
    with pytest.raises(AuthValidationError, match="Current password is incorrect"):
        accounts.auth.change_own_password(PasswordChangeRequest(current_password="wrong", new_password=NEW_PASSWORD, version=1), accounts.member.actor)
    events = accounts.db.fetch_all("SELECT action FROM auth_audit_events WHERE outcome='DENIED'")
    assert {row["action"] for row in events} == {"LOGIN_FAILED", "SELF_PASSWORD_CHANGE"}
    assert accounts.auth.verify_password(PASSWORD, user["password_hash"])


@pytest.mark.parametrize("operation", ["login", "password"])
def test_denial_audit_failure_does_not_commit_partial_security_state(accounts, monkeypatch, operation):
    before = snapshot(accounts)
    original = accounts.auth._audit

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected denial audit failure")

    monkeypatch.setattr(accounts.auth, "_audit", failed)
    with pytest.raises(RuntimeError, match="injected denial audit failure"):
        if operation == "login":
            accounts.auth.login("atomic.member", "wrong")
        else:
            accounts.auth.change_own_password(PasswordChangeRequest(current_password="wrong",
                new_password=NEW_PASSWORD, version=1), accounts.member.actor)
    assert snapshot(accounts) == before


@pytest.mark.parametrize("known", [False, True])
def test_concurrent_login_failures_cannot_lose_counts(accounts, known):
    peers = [accounts.peer() for _ in range(8)]
    barrier = threading.Barrier(len(peers))
    username = "atomic.member" if known else "missing.member"

    def fail(index):
        barrier.wait(timeout=5)
        with pytest.raises((AuthenticationError, AuthRateLimitError)):
            # Different IPs still share the account lock; nonexistent accounts
            # use the same rate key even across independent service instances.
            peers[index].login(username, "wrong", {"ip_address": f"192.0.2.{index}" if known else "192.0.2.1"})

    with ThreadPoolExecutor(max_workers=len(peers)) as pool:
        list(pool.map(fail, range(len(peers))))
    events = accounts.db.fetch_all("SELECT action FROM auth_audit_events WHERE outcome='DENIED'")
    assert len(events) == 8
    assert sum(row["action"] == "LOGIN_FAILED" for row in events) == 5
    if known:
        user = accounts.db.fetch_one("SELECT failed_login_count, locked_until FROM users WHERE id=?", (accounts.member.user["id"],))
        assert user["failed_login_count"] == 5 and user["locked_until"]
    else:
        row = accounts.db.fetch_one("SELECT attempts FROM auth_login_limits")
        assert row["attempts"] == 5


def test_stale_administrator_cannot_commit_after_revocation(accounts, monkeypatch):
    delegated = account(accounts, "delegated.admin", roles=["tenant_admin"])
    revoker, requester = accounts.peer(), accounts.peer()
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original = revoker._audit

    def held(*args, **kwargs):
        original(*args, **kwargs)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(revoker, "_audit", held)
    payload = UserCreate(username="stale.created", display_name="Must not exist", password=PASSWORD)

    def create():
        started.set()
        return requester.create_user(payload, delegated.actor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke = pool.submit(revoker.update_user, delegated.user["id"], UserUpdate(version=1, roles=["member"]), accounts.admin.actor)
        try:
            assert entered.wait(5)
            create_future = pool.submit(create)
            assert started.wait(5)
            with pytest.raises(FutureTimeout):
                create_future.result(timeout=0.1)
        finally:
            release.set()
        revoke.result(timeout=5)
        with pytest.raises(AuthenticationError):
            create_future.result(timeout=5)
    assert not accounts.db.fetch_one("SELECT id FROM users WHERE username='stale.created'")


def test_current_permissions_are_rechecked_even_if_a_session_snapshot_is_stale(accounts):
    delegated = account(accounts, "delegated.admin", roles=["tenant_admin"])
    accounts.db.execute("UPDATE users SET roles_json='[\"member\"]' WHERE id=?", (delegated.user["id"],))
    with pytest.raises(AuthAuthorizationError):
        accounts.auth.create_user(UserCreate(username="stale.created", display_name="No", password=PASSWORD), delegated.actor)


@pytest.mark.parametrize("stale", ["expired_session", "revoked_session", "disabled", "scope", "password_required"])
def test_mutation_rechecks_current_actor_session_and_account(accounts, stale):
    delegated = account(accounts, "delegated.admin", roles=["tenant_admin"])
    db = accounts.db
    if stale == "expired_session":
        db.execute("UPDATE auth_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (delegated.actor.session_id,))
    elif stale == "revoked_session":
        accounts.auth.revoke_session(delegated.actor.session_id)
    elif stale == "disabled":
        db.execute("UPDATE users SET status='INACTIVE' WHERE id=?", (delegated.user["id"],))
    elif stale == "scope":
        db.execute("UPDATE users SET tenant_id='tenant_elsewhere' WHERE id=?", (delegated.user["id"],))
    else:
        db.execute("UPDATE users SET must_change_password=1 WHERE id=?", (delegated.user["id"],))
    before = snapshot(accounts)
    with pytest.raises(AuthAuthorizationError if stale == "password_required" else AuthenticationError):
        accounts.auth.create_user(UserCreate(username="stale.created", display_name="No", password=PASSWORD), delegated.actor)
    assert snapshot(accounts) == before


def test_login_finishing_before_password_reset_is_revoked_atomically(accounts, monkeypatch):
    login_service, reset_service = accounts.peer(), accounts.peer()
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original = login_service.verify_password

    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(login_service, "verify_password", held)

    def reset():
        started.set()
        return reset_service.reset_password(accounts.member.user["id"],
            PasswordResetRequest(password=NEW_PASSWORD, version=1), accounts.admin.actor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        login = pool.submit(login_service.login, "atomic.member", PASSWORD)
        try:
            assert entered.wait(5)
            reset_future = pool.submit(reset)
            assert started.wait(5)
            with pytest.raises(FutureTimeout):
                reset_future.result(timeout=0.1)
        finally:
            release.set()
        session = login.result(timeout=5)
        reset_future.result(timeout=5)
    with pytest.raises(AuthenticationError):
        accounts.auth.authenticate(session["access_token"])
    with pytest.raises(AuthenticationError):
        accounts.auth.login("atomic.member", PASSWORD)


def test_login_after_reset_waits_and_rejects_the_old_password(accounts, monkeypatch):
    reset_service, login_service = accounts.peer(), accounts.peer()
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original = reset_service.revoke_user_sessions

    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(reset_service, "revoke_user_sessions", held)

    def login():
        started.set()
        return login_service.login("atomic.member", PASSWORD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reset = pool.submit(reset_service.reset_password, accounts.member.user["id"],
                            PasswordResetRequest(password=NEW_PASSWORD, version=1), accounts.admin.actor)
        try:
            assert entered.wait(5)
            login_future = pool.submit(login)
            assert started.wait(5)
            with pytest.raises(FutureTimeout):
                login_future.result(timeout=0.1)
        finally:
            release.set()
        reset.result(timeout=5)
        with pytest.raises(AuthenticationError):
            login_future.result(timeout=5)
    assert not accounts.db.fetch_one("SELECT id FROM auth_sessions WHERE user_id=? AND revoked_at IS NULL", (accounts.member.user["id"],))


def test_concurrent_bootstrap_creates_only_one_builtin_account(auth_store):
    peers = [auth_store.peer(), auth_store.peer()]
    barrier = threading.Barrier(2)

    def bootstrap(index):
        barrier.wait(timeout=5)
        return peers[index].bootstrap_super_admin(PASSWORD)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bootstrap, range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert auth_store.db.fetch_one("SELECT COUNT(*) AS count FROM users")["count"] == 1
    assert auth_store.db.fetch_one("SELECT COUNT(*) AS count FROM auth_audit_events WHERE action='USER_CREATED'")["count"] == 1


def test_concurrent_duplicate_creation_has_one_domain_conflict(auth_store):
    peers = [auth_store.peer(), auth_store.peer()]
    barrier = threading.Barrier(2)

    def create(index):
        barrier.wait(timeout=5)
        try:
            return peers[index].create_user(UserCreate(username="same.username", display_name="Same", password=PASSWORD))
        except AuthConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(result is not None for result in results) == 1
    assert auth_store.db.fetch_one("SELECT COUNT(*) AS count FROM auth_audit_events WHERE action='USER_CREATED'")["count"] == 1


def test_parallel_optimistic_updates_commit_exactly_one_audit(accounts):
    peers = [accounts.peer(), accounts.peer()]
    barrier = threading.Barrier(2)

    def update(index):
        barrier.wait(timeout=5)
        try:
            return peers[index].update_user(accounts.member.user["id"],
                UserUpdate(version=1, display_name=f"Version {index}"), accounts.admin.actor)
        except AuthConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert sum(result is not None for result in results) == 1
    assert accounts.db.fetch_one("SELECT COUNT(*) AS count FROM auth_audit_events WHERE action='USER_UPDATED'")["count"] == 1


def test_reciprocal_admin_changes_do_not_deadlock_or_use_revoked_authority(accounts):
    first = account(accounts, "first.admin", super_admin=True)
    second = account(accounts, "second.admin", super_admin=True)
    peers = [accounts.peer(), accounts.peer()]
    barrier = threading.Barrier(2)

    def demote(index):
        actor, target = (first, second) if index == 0 else (second, first)
        barrier.wait(timeout=5)
        try:
            return peers[index].update_user(target.user["id"], UserUpdate(version=1, is_super_admin=False), actor.actor)
        except AuthenticationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(demote, range(2)))
    assert sum(result is not None for result in results) == 1
