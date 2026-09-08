from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.coding.errors import SandboxUnavailableError
from packages.persistence.fencing import LeaseLostError


logger = logging.getLogger(__name__)
EXECUTING_STATUSES = {"CREATED", "QUEUED", "ORPHANED", "PREPARING", "RUNNING", "RESUMING"}


@dataclass(frozen=True)
class ExecutionLease:
    attempt_id: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class CancellationLease(ExecutionLease):
    """Accepted only by the fixed cancellation capture endpoint."""


class LeaseAuthority(Protocol):
    def lookup(self, sandbox_request_id: str) -> dict[str, Any] | None: ...
    def lookup_cancellation(self, sandbox_request_id: str) -> dict[str, Any] | None: ...
    def close(self) -> None: ...


class PostgresLeaseAuthority:
    """A read-only login with SELECT on the execution/cancellation views only."""

    def __init__(self, dsn: str, *, require_tls: bool = True):
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        options = conninfo_to_dict(dsn)
        if require_tls and options.get("sslmode") != "verify-full":
            raise ValueError("Sandbox lease authority requires PostgreSQL sslmode=verify-full")
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=4, timeout=3, open=True,
            kwargs={
                "autocommit": True, "row_factory": dict_row, "connect_timeout": 3,
                "options": (
                    options.get("options", "") +
                    " -c default_transaction_read_only=on -c statement_timeout=2500 -c lock_timeout=1000"
                ).strip(),
            },
        )
        try:
            self.pool.wait(timeout=5)
            with self.pool.connection() as connection:
                role = connection.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication FROM pg_roles WHERE rolname=current_user"
                ).fetchone()
                if not role or any(role.values()):
                    raise ValueError("Sandbox authority must use a dedicated unprivileged database role")
                accessible = connection.execute(
                    """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname=current_schema() AND c.relkind IN ('r','p')
                         AND has_table_privilege(current_user, c.oid, 'SELECT,INSERT,UPDATE,DELETE')
                       LIMIT 1"""
                ).fetchone()
                if accessible:
                    raise ValueError("Sandbox authority role must not have access to platform base tables")
                connection.execute("SELECT sandbox_request_id FROM sandbox_execution_leases LIMIT 0")
                connection.execute("SELECT sandbox_request_id FROM sandbox_cancellation_leases LIMIT 0")
        except BaseException:
            self.pool.close()
            raise

    def lookup(self, sandbox_request_id: str) -> dict[str, Any] | None:
        return self._lookup(sandbox_request_id, "sandbox_execution_leases")

    def lookup_cancellation(self, sandbox_request_id: str) -> dict[str, Any] | None:
        return self._lookup(sandbox_request_id, "sandbox_cancellation_leases")

    def _lookup(self, sandbox_request_id: str, view: str) -> dict[str, Any] | None:
        try:
            with self.pool.connection() as connection:
                return connection.execute(
                    f"""SELECT *, CAST(expires_at AS TIMESTAMPTZ)>clock_timestamp() AS lease_live
                       FROM {view} WHERE sandbox_request_id=%s""",
                    (sandbox_request_id,),
                ).fetchone()
        except Exception as exc:
            raise SandboxUnavailableError("Sandbox lease authority is unavailable") from exc

    def close(self) -> None:
        self.pool.close()


@dataclass
class _ExecutionState:
    request_id: str
    operation: asyncio.Lock = field(default_factory=asyncio.Lock)
    transition: asyncio.Lock = field(default_factory=asyncio.Lock)
    lease: ExecutionLease | None = None
    must_drain: bool = True
    pending_io: set[asyncio.Task] = field(default_factory=set)


class SandboxExecutionGate:
    """Serializes admission and drains previous processes before ownership changes.

    A watchdog can interrupt a running command without waiting for its operation
    lock. New operations cannot begin until both the interrupt and the previous
    operation have finished. No platform row lock is held during remote commands.
    """

    def __init__(self, authority: LeaseAuthority, provider: Any, *, interval: float = 0.5):
        self.authority = authority
        self.provider = provider
        self.interval = interval
        self.states: dict[str, _ExecutionState] = {}
        self.task: asyncio.Task | None = None

    async def validate(self, request_id: str, lease: ExecutionLease) -> dict[str, Any]:
        cancellation = isinstance(lease, CancellationLease)
        lookup = getattr(self.authority, 'lookup_cancellation', None) if cancellation else self.authority.lookup
        if lookup is None:
            raise LeaseLostError('Cancellation lease authority is unavailable')
        row = await asyncio.to_thread(lookup, request_id)
        if (
            not row or row["attempt_id"] != lease.attempt_id or not row.get("lease_live")
            or (row["run_status"] != 'CANCELLING' or row.get('finalization_status') != 'RUNNING'
                if cancellation else row["run_status"] not in EXECUTING_STATUSES)
            or not row.get("lease_token")
            or not hmac.compare_digest(str(row["lease_token"]), lease.token)
        ):
            raise LeaseLostError("Sandbox execution lease expired, was revoked, or was superseded")
        return row

    def state(self, external_id: str, request_id: str) -> _ExecutionState:
        state = self.states.setdefault(external_id, _ExecutionState(request_id))
        if state.request_id != request_id:
            raise LeaseLostError("Sandbox identity changed")
        return state

    @asynccontextmanager
    async def operation(self, external_id: str, request_id: str, lease: ExecutionLease | None, *, required: bool):
        if required and lease is None:
            raise LeaseLostError("Sandbox mutations require an execution lease")
        state = self.state(external_id, request_id)
        async with state.operation:
            async with state.transition:
                if lease:
                    await self.validate(request_id, lease)
                    if state.must_drain or state.lease != lease:
                        state.must_drain = True
                        await self.provider.interrupt(external_id)
                        await self.validate(request_id, lease)
                        state.lease = lease
                        state.must_drain = False
            try:
                yield
                if lease:
                    await self.validate(request_id, lease)
            except asyncio.CancelledError:
                # A disconnected HTTP client must not leave its command running.
                async with state.transition:
                    state.must_drain = True
                    await asyncio.shield(self.provider.interrupt(external_id))
                raise
            finally:
                # shielded offloads are not cancelled with the HTTP handler.
                # Drain them before allowing a replacement owner to enter.
                if state.pending_io:
                    await asyncio.shield(asyncio.gather(*state.pending_io, return_exceptions=True))
                    state.pending_io.clear()

    async def offload(self, external_id: str, request_id: str, function, *args, **kwargs):
        state = self.state(external_id, request_id)
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        state.pending_io.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                state.pending_io.discard(task)

    async def interrupt(self, external_id: str, request_id: str, *, attempt_id: str | None = None) -> bool:
        state = self.state(external_id, request_id)
        async with state.transition:
            if attempt_id:
                row = await asyncio.to_thread(self.authority.lookup, request_id)
                if row and row["attempt_id"] != attempt_id:
                    return False
            state.must_drain = True
            state.lease = None
            await self.provider.interrupt(external_id)
        # Wait for the old executor's IO to actually return before acknowledging.
        async with state.operation:
            pass
        return True

    async def start(self):
        self.task = asyncio.create_task(self._watch())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for external_id, state in tuple(self.states.items()):
            if state.lease:
                try:
                    await self.interrupt(external_id, state.request_id)
                except Exception:
                    logger.exception("Could not stop sandbox during service shutdown")

    async def _check_one(self, external_id: str, state: _ExecutionState):
        lease = state.lease
        if lease is None:
            return
        try:
            await self.validate(state.request_id, lease)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        async with state.transition:
            if state.lease != lease:
                return
            state.must_drain = True
            try:
                await self.provider.interrupt(external_id)
                state.lease = None
            except Exception:
                # Retain ownership marker so the next tick retries stopping.
                logger.exception("Could not stop sandbox after lease loss")

    async def _watch(self):
        while True:
            await asyncio.sleep(self.interval)
            await asyncio.gather(
                *(self._check_one(external_id, state) for external_id, state in tuple(self.states.items()))
            )
