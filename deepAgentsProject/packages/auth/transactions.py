"""Atomic authority for control-plane writes, not a background execution lease."""
from contextlib import contextmanager
from datetime import datetime

from packages.auth.permissions import authorize
from packages.auth.resource_access import refresh_context
from packages.auth.service import AuthAuthorizationError


def lock_accounts(db, user_ids):
    """Caller owns the transaction; use account governance's sorted row order."""
    if db.dialect == 'postgresql':
        for user_id in sorted(set(user_ids)):
            db.fetch_one('SELECT id FROM users WHERE id=? FOR UPDATE', (user_id,))


def _current_authority(db, context, permissions):
    current = refresh_context(db, context)
    authorize(current, *permissions)
    # Interactive sessions must also obey forced/expired password policy. A
    # background/proxy principal has no local password session to refresh.
    if current.session_id:
        user = db.fetch_one('SELECT must_change_password,password_expires_at FROM users WHERE id=?', (current.user_id,))
        if user and (user['must_change_password'] or (user['password_expires_at']
                and datetime.fromisoformat(user['password_expires_at']) <= db.current_time())):
            raise AuthAuthorizationError('Password change is required before accessing platform administration')
    return current


def current_authority(db, context, *permissions):
    """Read current interactive authority; writes must still use authorized_write."""
    return _current_authority(db, context, permissions)


@contextmanager
def authorized_write(db, context, *permissions, user_ids=()):
    """Serialize revocation with a whole write and its audit, or roll both back.

    Lock every affected account before tenant/resource rows. Existing release
    operations take their project coordination lock first; callers must not
    acquire that lock later while holding this scope. Keep network/compilation
    work outside and revalidate here before publishing its result.
    """
    with db.transaction():
        lock_accounts(db, [context.user_id, *user_ids])
        current = _current_authority(db, context, permissions)
        yield current
        # A clock-driven session/password expiry can happen while writing even
        # with rows locked. Do not commit an expired interactive authority.
        _current_authority(db, current, permissions)
