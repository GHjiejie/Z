"""Leased agent execution, metered model calls and conservative recovery."""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, insert, or_, select, update

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.platform import Platform, append_event, now, uid
from agent_platform.modules.runtime.engine import RunCancelled, execute_agent
from agent_platform.modules.runtime.gateway import Gateway, GatewayError, ModelReply

logger = logging.getLogger(__name__)


def sync_call_receipt(platform: Platform, receipt: dict) -> None:
    """Rebuild the display projection from the immutable billing evidence."""
    status = {
        "settled": "confirmed",
        "released": "released",
        "written_off": "written_off",
        "unresolved": "unresolved",
        "reserved": "running",
    }[receipt["status"]]
    raw = receipt.get("raw_usage")
    if isinstance(raw, str):
        raw = json.loads(raw)
    with platform.db.transaction(f"tenant:{receipt['tenant_id']}") as connection:
        connection.execute(
            update(t.calls)
            .where(
                t.calls.c.id == receipt["call_id"],
                t.calls.c.tenant_id == receipt["tenant_id"],
            )
            .values(
                status=status,
                input_tokens=receipt.get("input_tokens") or 0,
                output_tokens=receipt.get("output_tokens") or 0,
                cost=str(receipt["cost"]),
                provider_cost=receipt.get("provider_cost"),
                raw_usage=raw,
                error=receipt.get("reason", ""),
                finished_at=receipt.get("updated_at"),
            )
        )


class Worker:
    def __init__(self, platform: Platform, *, gateway=None):
        self.platform = platform
        self.db = platform.db
        self.settings = platform.settings
        # An explicitly injected adapter is only for offline execution fixtures.
        # Production resolves fresh tenant credentials for every external call.
        self.gateway = gateway
        self.owner = uid()
        self._claim_cursor = ""
        self._recover_cursor = ""
        self.max_run_cost = Decimal(self.settings.max_run_cost)
        if not self.max_run_cost.is_finite() or self.max_run_cost <= 0:
            raise ValueError("PLATFORM_MAX_RUN_COST must be a finite positive amount")

    def claim(self) -> dict | None:
        for tenant_id in self._tenant_page(self._claim_cursor, active=True, limit=128):
            self._claim_cursor = tenant_id
            with (
                self.db.tenant_scope(tenant_id),
                self.db.transaction(f"tenant:{tenant_id}") as connection,
            ):
                status = connection.scalar(
                    select(nt.tenant_settings.c.status).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                ceiling = connection.scalar(
                    select(nt.entitlements.c.max_concurrent_runs).where(
                        nt.entitlements.c.tenant_id == tenant_id
                    )
                )
                if status != "active" or ceiling is None or ceiling <= 0:
                    continue
                active = connection.scalar(
                    select(func.count())
                    .select_from(t.runs)
                    .where(
                        t.runs.c.tenant_id == tenant_id,
                        t.runs.c.status.in_(("running", "cancelling")),
                    )
                )
                if active >= ceiling:
                    continue
                row = (
                    connection.execute(
                        select(t.runs)
                        .where(
                            t.runs.c.tenant_id == tenant_id, t.runs.c.status == "queued"
                        )
                        .order_by(t.runs.c.created_at, t.runs.c.id)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    continue
                try:
                    self._authorize_identity(connection, dict(row))
                except PlatformError:
                    connection.execute(
                        update(t.runs)
                        .where(
                            t.runs.c.id == row["id"], t.runs.c.tenant_id == tenant_id
                        )
                        .values(
                            status="cancelled", cancel_requested=True, finished_at=now()
                        )
                    )
                    append_event(
                        connection,
                        row["id"],
                        "run.cancelled",
                        {"status": "cancelled", "error": "运行授权已失效。"},
                    )
                    continue
                changes = {
                    "status": "running",
                    "owner": self.owner,
                    "fence": row["fence"] + 1,
                    "lease_until": time.time() + 20,
                    "deadline": time.time() + self.settings.run_timeout,
                }
                result = connection.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == row["id"],
                        t.runs.c.tenant_id == tenant_id,
                        t.runs.c.status == "queued",
                    )
                    .values(**changes)
                )
                if result.rowcount:
                    return {**dict(row), **changes}
        return None

    def _tenant_page(self, cursor: str, *, active=False, limit=32) -> list[str]:
        """Global identity metadata only; tenant content is never scanned unscoped."""
        base = select(nt.tenant_settings.c.tenant_id)
        if active:
            base = base.where(nt.tenant_settings.c.status == "active")
        with self.db.read() as connection:
            identifiers = list(
                connection.scalars(
                    base.where(nt.tenant_settings.c.tenant_id > cursor)
                    .order_by(nt.tenant_settings.c.tenant_id)
                    .limit(limit)
                )
            )
            if len(identifiers) < limit and cursor:
                identifiers.extend(
                    connection.scalars(
                        base.where(nt.tenant_settings.c.tenant_id <= cursor)
                        .order_by(nt.tenant_settings.c.tenant_id)
                        .limit(limit - len(identifiers))
                    )
                )
        return identifiers

    def _authorize_identity(self, connection, run: dict) -> dict:
        identity = {"id": run["user_id"], "tenant_id": run["tenant_id"]}
        if run["spec"].get("membership_id"):
            identity["membership_id"] = run["spec"]["membership_id"]
        return self.platform.identity.assert_current(
            connection, identity, "runs.execute"
        )

    def _assert_lease(self, connection, run: dict):
        current = (
            connection.execute(
                select(t.runs).where(
                    t.runs.c.id == run["id"], t.runs.c.tenant_id == run["tenant_id"]
                )
            )
            .mappings()
            .one()
        )
        if (
            current["owner"] != self.owner
            or current["fence"] != run["fence"]
            or current["status"] not in ("running", "cancelling")
            or current["lease_until"] < time.time()
        ):
            raise PlatformError(409, "lease_lost", "运行执行权已失效。")
        return current

    def event(self, run: dict, kind: str, data: dict) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            self._assert_lease(connection, run)
            append_event(connection, run["id"], kind, data)

    def is_cancelled(self, run: dict) -> bool:
        with self.db.tenant_scope(run["tenant_id"]), self.db.read() as connection:
            current = (
                connection.execute(
                    select(t.runs).where(
                        t.runs.c.id == run["id"], t.runs.c.tenant_id == run["tenant_id"]
                    )
                )
                .mappings()
                .one()
            )
            try:
                self._authorize_identity(connection, run)
                active = True
            except PlatformError:
                active = False
        return bool(
            not active
            or current["cancel_requested"]
            or current["owner"] != self.owner
            or current["fence"] != run["fence"]
            or current["status"] not in ("running", "cancelling")
            or current["lease_until"] < time.time()
        )

    def renew(self, run: dict) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            self._assert_lease(connection, run)
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(lease_until=time.time() + 20)
            )

    async def heartbeat(self, run: dict) -> None:
        while True:
            await asyncio.sleep(3)
            await asyncio.to_thread(self.renew, run)

    def authorize_call(
        self, connection, run: dict, input_tokens: int, amount: Decimal
    ) -> None:
        current = self._assert_lease(connection, run)
        self._authorize_identity(connection, run)
        model = (
            connection.execute(
                select(t.models).where(
                    t.models.c.id == run["spec"]["model_id"],
                    t.models.c.tenant_id == run["tenant_id"],
                )
            )
            .mappings()
            .first()
        )
        if current["cancel_requested"]:
            raise PlatformError(403, "access_revoked", "账号已停用或运行已取消。")
        if (
            not model
            or not model["active"]
            or model["alias"] != run["spec"]["model"]["alias"]
        ):
            raise PlatformError(
                403, "model_disabled", "模型已停用或其部署发生变化，请重新运行。"
            )
        self._validate_route(connection, run)
        max_output = run["spec"]["max_tokens"]
        if (
            max_output > model["max_output_tokens"]
            or input_tokens + max_output > model["context_window"]
        ):
            raise PlatformError(
                400,
                "context_limit",
                "完整会话及工具定义超过模型上下文或输出限制，请新建会话。",
            )
        existing = list(
            connection.execute(
                select(reservations_table).where(
                    reservations_table.c.tenant_id == run["tenant_id"],
                    reservations_table.c.run_id == run["id"],
                )
            ).mappings()
        )
        committed = sum(
            (
                Decimal(str(r["cost"]))
                + (
                    Decimal(str(r["amount"]))
                    if r["status"] in ("reserved", "unresolved")
                    else Decimal(0)
                )
                for r in existing
            ),
            Decimal(0),
        )
        if committed + amount > self.max_run_cost:
            raise PlatformError(
                402,
                "run_budget_exceeded",
                f"本次运行达到 {self.max_run_cost} USD 的费用上限。",
            )

    def _validate_route(self, connection, run: dict) -> None:
        if self.gateway is not None and not run["spec"].get("deployment_id"):
            return  # Offline fixtures have no external deployment or credentials.
        row = (
            connection.execute(
                select(
                    gt.model_bindings.c.enabled,
                    gt.deployments.c.id,
                    gt.deployments.c.internal_route,
                    gt.deployments.c.status,
                    gt.deployments.c.config_version,
                    gt.deployments.c.owner_scope,
                    gt.deployments.c.owner_tenant_id,
                )
                .join(
                    gt.deployments,
                    gt.model_bindings.c.deployment_id == gt.deployments.c.id,
                )
                .where(
                    gt.model_bindings.c.tenant_id == run["tenant_id"],
                    gt.model_bindings.c.model_id == run["spec"]["model_id"],
                )
            )
            .mappings()
            .first()
        )
        if (
            not row
            or not row["enabled"]
            or row["status"] != "active"
            or (
                row["owner_scope"] == "tenant"
                and row["owner_tenant_id"] != run["tenant_id"]
            )
            or row["id"] != run["spec"].get("deployment_id")
            or (row["internal_route"] != run["spec"].get("route"))
            or (
                run["spec"].get("config_version") is not None
                and row["config_version"] != run["spec"]["config_version"]
            )
        ):
            raise PlatformError(
                403, "model_grant_revoked", "模型授权或部署配置已变化，请重新发起运行。"
            )

    def _resolve_inference(self, run: dict) -> tuple[object, dict]:
        with self.db.tenant_scope(run["tenant_id"]):
            if self.gateway is not None:
                return self.gateway, {
                    "deployment_id": run["spec"].get("deployment_id"),
                    "route": run["spec"].get("route", run["spec"]["model"]["alias"]),
                    "credential_version_id": None,
                    "price_version_id": run["spec"].get("price_version_id"),
                    "config_version": run["spec"].get("config_version", 1),
                }
            routing = self.platform.gateway.resolve(
                run["tenant_id"],
                {
                    **run["spec"]["model"],
                    "deployment_id": run["spec"].get("deployment_id"),
                },
            )
            if routing["deployment_id"] != run["spec"].get("deployment_id") or (
                routing["route"] != run["spec"].get("route")
            ):
                raise PlatformError(
                    403, "deployment_changed", "模型部署已变化，请重新发起运行。"
                )
            gateway = Gateway(
                routing["base_url"],
                routing["api_key"],
                timeout=self.settings.model_timeout,
                local_address=self.settings.gateway_local_address,
            )
            # Credentials remain solely in the transient inference adapter.
            return gateway, {
                key: routing.get(key)
                for key in (
                    "deployment_id",
                    "route",
                    "credential_version_id",
                    "price_version_id",
                    "config_version",
                )
            }

    def _record_attempt(self, run: dict, call_id: str, routing: dict) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run)
            identity = self._authorize_identity(connection, run)
            if current["cancel_requested"]:
                raise RunCancelled()
            self._validate_route(connection, run)
            if routing["credential_version_id"]:
                binding = (
                    connection.execute(
                        select(gt.bindings).where(
                            gt.bindings.c.tenant_id == run["tenant_id"],
                            gt.bindings.c.active_credential_version_id
                            == routing["credential_version_id"],
                            gt.bindings.c.status == "ready",
                        )
                    )
                    .mappings()
                    .first()
                )
                credential = (
                    connection.execute(
                        select(gt.credentials).where(
                            gt.credentials.c.tenant_id == run["tenant_id"],
                            gt.credentials.c.id == routing["credential_version_id"],
                            gt.credentials.c.status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if not binding or not credential:
                    raise PlatformError(
                        403,
                        "gateway_credential_changed",
                        "调用凭据已变化，请重新发起运行。",
                    )
            if routing["deployment_id"] is None and self.gateway is not None:
                return
            connection.execute(
                insert(rt.call_attempts).values(
                    id=call_id,
                    tenant_id=run["tenant_id"],
                    run_id=run["id"],
                    membership_id=identity["membership_id"],
                    deployment_id=routing["deployment_id"],
                    credential_version_id=routing["credential_version_id"],
                    price_version_id=run["spec"].get("price_version_id")
                    or routing["price_version_id"],
                    config_version=run["spec"].get(
                        "config_version", routing["config_version"]
                    ),
                    route=routing["route"],
                    created_at=now(),
                )
            )

    def register_call(self, run: dict, call_id: str) -> None:
        model = run["spec"]["model"]
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            self._assert_lease(connection, run)
            user = (
                connection.execute(
                    select(t.users).where(t.users.c.id == run["user_id"])
                )
                .mappings()
                .one()
            )
            connection.execute(
                insert(t.calls).values(
                    id=call_id,
                    tenant_id=run["tenant_id"],
                    user_id=run["user_id"],
                    run_id=run["id"],
                    model_id=model["id"],
                    model=model["alias"],
                    agent_name=run["agent_name"],
                    user_email=user["email"],
                    status="admitting",
                    input_tokens=0,
                    output_tokens=0,
                    cost="0",
                    provider_cost=None,
                    price_version=model["price_version"],
                    error="",
                    created_at=now(),
                )
            )

    async def invoke(
        self, run: dict, messages: list[dict], tools: list[dict]
    ) -> ModelReply:
        with self.db.tenant_scope(run["tenant_id"]):
            return await self._invoke(run, messages, tools)

    async def _invoke(
        self, run: dict, messages: list[dict], tools: list[dict]
    ) -> ModelReply:
        call_id = uid()
        model = run["spec"]["model"]
        max_tokens = run["spec"]["max_tokens"]
        # Conservative UTF-8 byte bound, plus protocol framing. No cache discount
        # is assumed. Gateway usage determines platform settlement, and may be
        # estimated by LiteLLM when the provider omits its own usage.
        estimate = (
            len(
                json.dumps(
                    {"messages": messages, "tools": tools}, ensure_ascii=False
                ).encode()
            )
            + 256
            + 32 * len(messages)
        )
        amount = (
            Decimal(model["input_price"]) * estimate
            + Decimal(model["output_price"]) * max_tokens
        ) / Decimal(1000000)
        await asyncio.to_thread(self.register_call, run, call_id)
        billing = BillingService(
            self.db,
            self.settings.redis_url,
            authorize=lambda connection: self.authorize_call(
                connection, run, estimate, amount
            ),
        )
        reserved = False
        sent = False
        receipt = None
        reply = None
        try:
            # Shield transaction completion so cancellation cannot orphan a newly
            # committed reservation whose result the coroutine never received.
            reservation_task = asyncio.create_task(
                asyncio.to_thread(
                    billing.reserve,
                    run["tenant_id"],
                    run["user_id"],
                    model["id"],
                    run["id"],
                    call_id,
                    estimate,
                    max_tokens,
                    model["input_price"],
                    model["output_price"],
                )
            )
            try:
                receipt = await asyncio.shield(reservation_task)
                reserved = True
            except asyncio.CancelledError:
                receipt = await reservation_task
                reserved = True
                raise
            if await asyncio.to_thread(self.is_cancelled, run):
                raise RunCancelled()
            inference, routing = await asyncio.to_thread(self._resolve_inference, run)
            await asyncio.to_thread(self._record_attempt, run, call_id, routing)
            await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            await asyncio.to_thread(
                self.event,
                run,
                "model.started",
                {"call_id": call_id, "model": model["alias"]},
            )
            sent = True
            buffer = ""
            flushed_at = time.monotonic()
            async for event in inference.stream(
                messages,
                routing["route"],
                max_tokens,
                run["spec"]["temperature"],
                tools,
            ):
                if event.type == "text":
                    buffer += event.data["text"]
                    if len(buffer) >= 128 or time.monotonic() - flushed_at > 0.1:
                        await asyncio.to_thread(
                            self.event,
                            run,
                            "message.delta",
                            {"text": buffer, "call_id": call_id},
                        )
                        buffer, flushed_at = "", time.monotonic()
                elif event.type == "result":
                    reply = event.data
            if reply is None:
                raise GatewayError(
                    "usage_unavailable", "模型未返回完整用量，费用等待核实。"
                )
            # Settlement does not depend on current user permissions: a revoked
            # account must still pay for usage already produced upstream.
            settlement = asyncio.create_task(
                asyncio.to_thread(
                    self.platform.billing.settle,
                    run["tenant_id"],
                    call_id,
                    reply.input_tokens,
                    reply.output_tokens,
                    None,
                    reply.raw_usage,
                )
            )
            try:
                receipt = await asyncio.shield(settlement)
            except asyncio.CancelledError:
                receipt = await settlement
                await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
                raise
            await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            if buffer:
                await asyncio.to_thread(
                    self.event,
                    run,
                    "message.delta",
                    {"text": buffer, "call_id": call_id},
                )
            await asyncio.to_thread(
                self.event,
                run,
                "usage.updated",
                {
                    "call_id": call_id,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "cost": receipt["cost"],
                    "status": "confirmed",
                },
            )
            return reply
        except BaseException as exc:
            if isinstance(exc, GatewayError) and not exc.request_sent:
                sent = False
            if reserved:
                if reply is not None:
                    receipt = await asyncio.to_thread(
                        self.platform.billing.settle,
                        run["tenant_id"],
                        call_id,
                        reply.input_tokens,
                        reply.output_tokens,
                        None,
                        reply.raw_usage,
                    )
                else:
                    operation = (
                        self.platform.billing.unresolved
                        if sent
                        else self.platform.billing.release
                    )
                    reason = (
                        "上游可能已执行，等待用量证据。"
                        if sent
                        else "请求在发送前终止。"
                    )
                    receipt = await asyncio.to_thread(
                        operation, run["tenant_id"], call_id, reason
                    )
                await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            else:
                with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
                    connection.execute(
                        update(t.calls)
                        .where(t.calls.c.id == call_id)
                        .values(
                            status="rejected",
                            error=getattr(exc, "message", "请求准入失败。"),
                            finished_at=now(),
                        )
                    )
            raise

    def finish(self, run: dict, status: str, result: str = "", error: str = "") -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run)
            if current["cancel_requested"] and status == "succeeded":
                status = "cancelled"
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(status=status, error=error, finished_at=now(), lease_until=None)
            )
            if status == "succeeded":
                connection.execute(
                    insert(t.messages).values(
                        id=uid(),
                        tenant_id=run["tenant_id"],
                        created_at=now(),
                        session_id=run["session_id"],
                        run_id=run["id"],
                        role="assistant",
                        content=result,
                    )
                )
            kind = "run.completed" if status == "succeeded" else f"run.{status}"
            append_event(
                connection,
                run["id"],
                kind,
                {
                    "status": status,
                    "content": result if status == "succeeded" else "",
                    "error": error,
                },
            )

    async def execute(self, run: dict) -> None:
        with self.db.tenant_scope(run["tenant_id"]):
            await self._execute(run)

    async def _execute(self, run: dict) -> None:
        pulse = asyncio.create_task(self.heartbeat(run))
        try:
            with self.db.read() as connection:
                messages = [
                    {"role": row["role"], "content": row["content"]}
                    for row in connection.execute(
                        select(t.messages)
                        .where(
                            t.messages.c.session_id == run["session_id"],
                            t.messages.c.tenant_id == run["tenant_id"],
                        )
                        .order_by(t.messages.c.created_at)
                    ).mappings()
                ]
            await asyncio.to_thread(
                self.event, run, "run.started", {"status": "running"}
            )

            async def invoke(messages, tools):
                return await self.invoke(run, messages, tools)

            async def emit(kind, data):
                await asyncio.to_thread(self.event, run, kind, data)

            async def cancelled():
                return await asyncio.to_thread(self.is_cancelled, run)

            async with asyncio.timeout(self.settings.run_timeout):
                result = await execute_agent(
                    run["spec"], messages, invoke, emit, cancelled
                )
            await asyncio.to_thread(self.finish, run, "succeeded", result)
        except RunCancelled as exc:
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish, run, "cancelled", error=exc.message
                )
        except TimeoutError:
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "expired",
                    error="运行超时，已产生的用量仍会结算。",
                )
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "failed",
                    error="执行进程停止，未确认费用等待对账；未自动重放模型请求。",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate failed jobs without leaking provider errors
            logger.warning(
                "run failed run_id=%s error=%s", run["id"], type(exc).__name__
            )
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "failed",
                    error=getattr(
                        exc, "message", "运行失败，请检查模型配置或联系管理员。"
                    ),
                )
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pulse

    def recover(self) -> None:
        """Bounded tenant batches repair evidence without replaying requests."""
        for tenant_id in self._tenant_page(self._recover_cursor, limit=32):
            self._recover_cursor = tenant_id
            with self.db.tenant_scope(tenant_id):
                self._recover_tenant(tenant_id)

    def _recover_tenant(self, tenant_id: str) -> None:
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=self.settings.run_timeout)
        ).isoformat()
        terminal = ("failed", "cancelled", "expired", "succeeded")
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            due = list(
                connection.execute(
                    select(t.runs)
                    .where(
                        t.runs.c.tenant_id == tenant_id,
                        or_(
                            and_(
                                t.runs.c.status == "queued",
                                t.runs.c.created_at < cutoff,
                            ),
                            and_(
                                t.runs.c.status.in_(("running", "cancelling")),
                                or_(
                                    t.runs.c.lease_until < time.time(),
                                    t.runs.c.lease_until.is_(None),
                                ),
                            ),
                        ),
                    )
                    .order_by(t.runs.c.created_at, t.runs.c.id)
                    .limit(64)
                ).mappings()
            )
            for run in due:
                queued = run["status"] == "queued"
                state = "expired" if queued else "failed"
                message = (
                    "排队超时，未调用模型。" if queued else "执行进程失联，未自动重放。"
                )
                connection.execute(
                    update(t.runs)
                    .where(t.runs.c.tenant_id == tenant_id, t.runs.c.id == run["id"])
                    .values(
                        status=state,
                        fence=run["fence"] + 1,
                        error=message,
                        lease_until=None,
                        finished_at=now(),
                    )
                )
                append_event(
                    connection,
                    run["id"],
                    f"run.{state}",
                    {"status": state, "error": message},
                )

        mismatches = [
            and_(reservations_table.c.status == stored, t.calls.c.status != projected)
            for stored, projected in (
                ("settled", "confirmed"),
                ("released", "released"),
                ("written_off", "written_off"),
                ("unresolved", "unresolved"),
                ("reserved", "running"),
            )
        ]
        with self.db.read() as connection:
            receipts = list(
                connection.execute(
                    select(reservations_table, t.runs.c.status.label("run_status"))
                    .join(
                        t.runs,
                        and_(
                            t.runs.c.id == reservations_table.c.run_id,
                            t.runs.c.tenant_id == reservations_table.c.tenant_id,
                        ),
                    )
                    .outerjoin(
                        t.calls,
                        and_(
                            t.calls.c.id == reservations_table.c.call_id,
                            t.calls.c.tenant_id == reservations_table.c.tenant_id,
                        ),
                    )
                    .where(
                        reservations_table.c.tenant_id == tenant_id,
                        or_(
                            and_(
                                reservations_table.c.status == "reserved",
                                t.runs.c.status.in_(terminal),
                            ),
                            *mismatches,
                        ),
                    )
                    .order_by(reservations_table.c.updated_at, reservations_table.c.id)
                    .limit(64)
                ).mappings()
            )
        for row in receipts:
            if row["status"] == "reserved" and row["run_status"] in terminal:
                receipt = self.platform.billing.unresolved(
                    tenant_id, row["call_id"], "执行结束但缺少结算证据。"
                )
            else:
                from agent_platform.modules.billing.service import _serialize

                receipt = _serialize(dict(row))
            sync_call_receipt(self.platform, receipt)
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            orphan_ids = list(
                connection.scalars(
                    select(t.calls.c.id)
                    .join(
                        t.runs,
                        and_(
                            t.runs.c.id == t.calls.c.run_id,
                            t.runs.c.tenant_id == t.calls.c.tenant_id,
                        ),
                    )
                    .outerjoin(
                        reservations_table,
                        and_(
                            reservations_table.c.call_id == t.calls.c.id,
                            reservations_table.c.tenant_id == t.calls.c.tenant_id,
                        ),
                    )
                    .where(
                        t.calls.c.tenant_id == tenant_id,
                        t.calls.c.status == "admitting",
                        t.runs.c.status.in_(terminal),
                        reservations_table.c.id.is_(None),
                    )
                    .order_by(t.calls.c.created_at, t.calls.c.id)
                    .limit(64)
                )
            )
            if orphan_ids:
                connection.execute(
                    update(t.calls)
                    .where(
                        t.calls.c.tenant_id == tenant_id,
                        t.calls.c.id.in_(orphan_ids),
                        t.calls.c.status == "admitting",
                    )
                    .values(
                        status="rejected",
                        error="准入完成前运行已终止，未产生费用。",
                        finished_at=now(),
                    )
                )

    async def serve(self, stop: asyncio.Event) -> None:
        tasks: set[asyncio.Task] = set()
        recovered_at = 0.0
        try:
            while not stop.is_set():
                try:
                    if time.monotonic() - recovered_at > 10:
                        await asyncio.to_thread(self.recover)
                        recovered_at = time.monotonic()
                    while len(tasks) < self.settings.worker_concurrency:
                        run = await asyncio.to_thread(self.claim)
                        if not run:
                            break
                        task = asyncio.create_task(self.execute(run))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                except Exception as exc:  # noqa: BLE001 - background supervisor retries storage availability
                    logger.error("worker cycle failed: %s", type(exc).__name__)
                await asyncio.sleep(self.settings.worker_poll_seconds)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
