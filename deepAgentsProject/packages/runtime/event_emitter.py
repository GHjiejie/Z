from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

from packages.domain.models import RuntimeEvent, utc_now
from packages.persistence import Database


class EventEmitter:
    def __init__(self, db: Database):
        self.db = db

    def append(
        self,
        run_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        execution_path: Optional[List[str]] = None,
        visibility: str = "user",
    ) -> Dict[str, Any]:
        with self.db.lock:
            run_row = self.db.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run_row:
                raise ValueError(f"Run {run_id} does not exist")
            run = dict(run_row)
            row = self.db.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            event = RuntimeEvent(
                event_id=f"evt_{secrets.token_hex(8)}",
                sequence=sequence,
                type=event_type,
                tenant_id=run["tenant_id"],
                project_id=run["project_id"],
                thread_id=run["thread_id"],
                run_id=run_id,
                attempt_id=run["current_attempt_id"],
                span_id=span_id,
                parent_span_id=parent_span_id,
                execution_path=execution_path or ["main"],
                occurred_at=utc_now(),
                visibility=visibility,
                payload=payload or {},
            ).model_dump()
            self.db.connection.execute(
                "INSERT INTO run_events (event_id, run_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (event["event_id"], run_id, sequence, self.db.encode(event), event["occurred_at"]),
            )
            self.db.connection.commit()
            return event

    def list(self, run_id: str, after_sequence: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            """SELECT event_json FROM run_events WHERE run_id=? AND sequence>?
               ORDER BY sequence ASC LIMIT ?""",
            (run_id, after_sequence, limit),
        )
        return [row["event"] for row in rows]

