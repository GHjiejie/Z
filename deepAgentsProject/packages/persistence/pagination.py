from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime


class InvalidCursor(ValueError):
    pass


class PageAccessChanged(RuntimeError):
    pass


def _scope(resource, context):
    return hashlib.sha256(json.dumps([resource, context.tenant_id, context.project_id,
        context.environment_id, context.user_id], separators=(",", ":")).encode()).hexdigest()


def _decode(cursor, resource, context):
    if not cursor:
        return None
    try:
        if len(cursor) > 1024:
            raise ValueError()
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if set(data) != {"v", "scope", "after"} or data["v"] != 1 or data["scope"] != _scope(resource, context):
            raise ValueError()
        after = data["after"]
        if not isinstance(after, list) or len(after) != 2:
            raise ValueError()
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in after):
            raise ValueError()
        datetime.fromisoformat(after[0])
        return after
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise InvalidCursor("Invalid cursor or changed list scope; reload the first page") from exc


def _encode(row, resource, context):
    data = {"v": 1, "scope": _scope(resource, context), "after": [row["created_at"], row["id"]]}
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def authorized_page(db, *, query, params, alias, resource, context, visible, limit=100, cursor=None):
    """Stable created-at/id keysets, filled from authorized rows only.

    The query must contain WHERE and scope predicates. Source ACLs can change
    dynamically and cannot safely be approximated by pagination over raw rows.
    Scan bounded batches, stop after limit+1 visible rows, and never put an
    inaccessible row's position in the public cursor. A cursor is a position,
    not an authority: all scope and source checks run again on every page.
    """
    if alias not in {"r", "t", "d"} or not 1 <= limit <= 500:
        raise ValueError("Invalid pagination configuration")
    after = _decode(cursor, resource, context)
    rows = []
    batch_size = max(50, min(limit + 1, 500))
    while len(rows) <= limit:
        boundary = ""
        values = tuple(params)
        if after:
            boundary = f" AND ({alias}.created_at<? OR ({alias}.created_at=? AND {alias}.id<?))"
            values += (after[0], after[0], after[1])
        batch = db.fetch_all(query + boundary + f" ORDER BY {alias}.created_at DESC,{alias}.id DESC LIMIT ?",
                             (*values, batch_size))
        for row in batch:
            if visible(row):
                rows.append(row)
                if len(rows) > limit:
                    break
        if len(rows) > limit or len(batch) < batch_size:
            break
        after = [batch[-1]["created_at"], batch[-1]["id"]]
    items = rows[:limit]
    # Conceal records revoked while another row was being read; callers can
    # retry with a fresh page instead of receiving a partial or leaking cursor.
    if any(not visible(row) for row in items):
        raise PageAccessChanged("Access changed while listing resources; reload the page")
    more = len(rows) > limit
    return {"items": items, "has_more": more,
            "next_cursor": _encode(items[-1], resource, context) if more else None}
