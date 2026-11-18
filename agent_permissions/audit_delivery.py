"""Lease and deliver local evidence without holding a business database transaction over HTTP."""

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from agent_permissions.audit_contract import Batch
from agent_permissions.audit_store import AuditStore
from agent_permissions.config import Settings
from agent_permissions.database import create_database
from agent_permissions.errors import ApiError

logger = logging.getLogger(__name__)
PREFIX = "/internal/v1/agent-audit"


def audit_store(settings: Settings) -> AuditStore:
    path = settings.audit_database_path or settings.database_path.with_suffix(
        ".audit.sqlite3"
    )
    if path.resolve() == settings.database_path.resolve() or (
        settings.audit_database_url
        and settings.audit_database_url == settings.database_url
    ):
        raise ValueError("审计库必须使用独立数据库")
    return AuditStore(
        create_database(
            replace(
                settings, database_path=path, database_url=settings.audit_database_url
            )
        )
    )


def validate_credentials(settings: Settings):
    if (
        not settings.audit_write_token
        or not settings.audit_read_token
        or min(len(settings.audit_write_token), len(settings.audit_read_token)) < 32
        or settings.audit_read_token == settings.audit_write_token
    ):
        raise ValueError("请为审计投递和查询各配置一个独立的至少 32 字符凭据")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", settings.audit_producer_id):
        raise ValueError("无效的审计生产方标识")


class AuditDelivery:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.local = None if settings.audit_service_url else audit_store(settings)
        self.client = None
        self.task = None
        self.local_ready = False
        self.init_lock = asyncio.Lock()

    async def start(self):
        if self.settings.audit_service_url:
            validate_credentials(self.settings)
            url = urlsplit(self.settings.audit_service_url)
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or (
                    url.scheme != "https"
                    and not (
                        url.scheme == "http"
                        and url.hostname in {"127.0.0.1", "localhost", "::1"}
                    )
                )
            ):
                raise ValueError("审计服务地址须使用 HTTPS 或本地环回 HTTP")
            self.client = httpx.AsyncClient(
                base_url=self.settings.audit_service_url, timeout=5, trust_env=False
            )
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.aclose()
        if self.local:
            await run_in_threadpool(self.local.database.close)

    async def ensure_local(self):
        async with self.init_lock:
            if not self.local_ready:
                await run_in_threadpool(
                    self.local.initialize,
                    managed=self.settings.audit_store_mode == "managed",
                )
                self.local_ready = True

    def claim(self, now):
        query = "SELECT event_id,payload,attempts FROM agent_audit_outbox WHERE state='pending' AND available_at<=? ORDER BY available_at,created_at,event_id LIMIT 100"
        with self.database.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM agent_audit_outbox WHERE state='pending' AND available_at<=? LIMIT 1",
                (now,),
            ).fetchone():
                return None, []
        lease = uuid4().hex
        with self.database.transaction(write=True) as conn:
            rows = [dict(row) for row in conn.execute(query, (now,))]
            conn.executemany(
                "UPDATE agent_audit_outbox SET lease=?,available_at=?,attempts=attempts+1 WHERE event_id=?",
                [(lease, now + 30, row["event_id"]) for row in rows],
            )
        return lease, rows

    def acknowledge(self, lease, rows, statuses, now):
        with self.database.transaction(write=True) as conn:
            for row in rows:
                status = statuses.get(row["event_id"])
                state = (
                    "delivered"
                    if status in {"saved", "duplicate"}
                    else "quarantined"
                    if status in {"conflict", "rejected"}
                    else "pending"
                )
                if state == "quarantined":
                    logger.error("Audit event quarantined event_id=%s", row["event_id"])
                conn.execute(
                    """UPDATE agent_audit_outbox SET state=?,lease=NULL,delivered_at=?,available_at=?
                    WHERE event_id=? AND lease=?""",
                    (
                        state,
                        now if state == "delivered" else None,
                        now + min(300, 2 ** min(9, row["attempts"])),
                        row["event_id"],
                        lease,
                    ),
                )

    async def flush(self):
        lease, rows = await run_in_threadpool(self.claim, time.time())
        if not rows:
            return 0
        statuses = {}
        try:
            events = []
            from agent_permissions.audit_contract import Event

            for row in rows:
                try:
                    events.append(Event.model_validate_json(row["payload"]))
                except ValidationError:
                    statuses[row["event_id"]] = "rejected"
            if events:
                batch = Batch(
                    schema_version=1,
                    producer_id=self.settings.audit_producer_id,
                    events=events,
                )
                if self.local:
                    await self.ensure_local()
                    acks = await run_in_threadpool(self.local.receive, batch)
                else:
                    response = await self.client.post(
                        PREFIX + "/events",
                        headers=self.headers(write=True),
                        json=batch.model_dump(exclude_none=True, exclude_defaults=True),
                    )
                    response.raise_for_status()
                    acks = response.json()["acknowledgements"]
                for item in acks:
                    if "event_id" in item:
                        statuses[item["event_id"]] = item["status"]
                    elif (
                        item.get("status") == "rejected"
                        and isinstance(item.get("index"), int)
                        and 0 <= item["index"] < len(events)
                    ):
                        statuses[events[item["index"]].event_id] = "rejected"
        finally:
            await run_in_threadpool(
                self.acknowledge, lease, rows, statuses, time.time()
            )
        return len(rows)

    def headers(self, *, write=False):
        token = (
            self.settings.audit_write_token if write else self.settings.audit_read_token
        )
        return {"Authorization": "Bearer " + token}

    async def read(self, endpoint, query=None, call_id=None):
        try:
            if self.local:
                await self.ensure_local()
                if call_id is not None:
                    return await run_in_threadpool(self.local.detail, call_id)
                return await run_in_threadpool(self.local.page, query)
            params = query.model_dump(mode="json", exclude_none=True) if query else {}
            response = await self.client.get(
                PREFIX + endpoint, params=params, headers=self.headers()
            )
            if response.status_code in {404, 422}:
                raise ApiError(
                    response.status_code,
                    "AUDIT_OPERATION_NOT_FOUND"
                    if response.status_code == 404
                    else "INVALID_QUERY",
                    "未找到该操作或查询参数无效",
                )
            response.raise_for_status()
            return response.json()
        except ApiError:
            raise
        except Exception:  # noqa: BLE001 -- a disconnected collector cannot stop the business API
            raise ApiError(503, "AUDIT_UNAVAILABLE", "审计查询暂时不可用") from None

    def maintenance(self):
        now = time.time()
        with self.database.transaction(write=True) as conn:
            conn.execute(
                """DELETE FROM agent_audit_outbox WHERE state='delivered' AND delivered_at<?
                AND call_id IN (SELECT call_id FROM agent_audit_call_refs
                WHERE state IN ('completed','failed','rejected','cancelled','expired'))""",
                (now - self.settings.audit_outbox_days * 86400,),
            )
            conn.execute("""DELETE FROM agent_audit_call_refs WHERE (run_id IS NULL OR run_id IN
                (SELECT id FROM agent_chat_runs WHERE purged=1)) AND NOT EXISTS
                (SELECT 1 FROM agent_audit_outbox o WHERE o.call_id=agent_audit_call_refs.call_id)""")
        if (
            self.local
            and self.local_ready
            and self.settings.audit_store_mode == "automatic"
        ):
            self.local.cleanup(datetime.now(UTC), self.settings.audit_retention_days)

    async def run(self):
        next_cleanup = time.monotonic() + 60
        while True:
            delay = 1
            try:
                if await self.flush() == 100:
                    delay = 0
                if time.monotonic() >= next_cleanup:
                    await run_in_threadpool(self.maintenance)
                    next_cleanup = time.monotonic() + 3600
            except Exception:  # noqa: BLE001 -- a disconnected collector cannot stop the business API
                logger.warning(
                    "Audit delivery unavailable; pending records will be retried"
                )
            await asyncio.sleep(delay)
