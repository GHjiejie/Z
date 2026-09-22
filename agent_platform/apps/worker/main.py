"""Leased agent execution, metered model calls and conservative recovery."""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, select, update

from agent_platform.infrastructure import tables as t
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
        self.gateway = gateway or Gateway(
            self.settings.litellm_url,
            self.settings.litellm_key,
            timeout=self.settings.model_timeout,
        )
        self.owner = uid()
        self.max_run_cost = Decimal(self.settings.max_run_cost)
        if not self.max_run_cost.is_finite() or self.max_run_cost <= 0:
            raise ValueError("PLATFORM_MAX_RUN_COST must be a finite positive amount")

    def claim(self) -> dict | None:
        with self.db.transaction("scheduler") as connection:
            candidates = list(
                connection.execute(
                    select(t.runs)
                    .where(t.runs.c.status == "queued")
                    .order_by(t.runs.c.created_at)
                    .limit(128)
                ).mappings()
            )
            active = dict(
                connection.execute(
                    select(t.runs.c.tenant_id, func.count())
                    .where(t.runs.c.status.in_(["running", "cancelling"]))
                    .group_by(t.runs.c.tenant_id)
                ).all()
            )
            if not candidates:
                return None
            # Prefer tenants with fewer running jobs, then oldest queued request.
            row = min(
                candidates,
                key=lambda r: (active.get(r["tenant_id"], 0), r["created_at"]),
            )
            changes = {
                "status": "running",
                "owner": self.owner,
                "fence": row["fence"] + 1,
                "lease_until": time.time() + 20,
                "deadline": time.time() + self.settings.run_timeout,
            }
            result = connection.execute(
                update(t.runs)
                .where(t.runs.c.id == row["id"], t.runs.c.status == "queued")
                .values(**changes)
            )
            return {**dict(row), **changes} if result.rowcount else None

    def _assert_lease(self, connection, run: dict):
        current = (
            connection.execute(select(t.runs).where(t.runs.c.id == run["id"]))
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
        with self.db.read() as connection:
            current = (
                connection.execute(select(t.runs).where(t.runs.c.id == run["id"]))
                .mappings()
                .one()
            )
            active = connection.scalar(
                select(t.users.c.active).where(t.users.c.id == run["user_id"])
            )
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
        user = (
            connection.execute(
                select(t.users).where(
                    t.users.c.id == run["user_id"],
                    t.users.c.tenant_id == run["tenant_id"],
                )
            )
            .mappings()
            .one()
        )
        model = (
            connection.execute(
                select(t.models).where(
                    t.models.c.id == run["spec"]["model_id"],
                    t.models.c.tenant_id == run["tenant_id"],
                )
            )
            .mappings()
            .one()
        )
        if not user["active"] or current["cancel_requested"]:
            raise PlatformError(403, "access_revoked", "账号已停用或运行已取消。")
        if not model["active"] or model["alias"] != run["spec"]["model"]["alias"]:
            raise PlatformError(
                403, "model_disabled", "模型已停用或其部署发生变化，请重新运行。"
            )
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
            async for event in self.gateway.stream(
                messages, model["alias"], max_tokens, run["spec"]["temperature"], tools
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
        pulse = asyncio.create_task(self.heartbeat(run))
        try:
            with self.db.read() as connection:
                messages = [
                    {"role": row["role"], "content": row["content"]}
                    for row in connection.execute(
                        select(t.messages)
                        .where(t.messages.c.session_id == run["session_id"])
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
        """Never replay an external request after losing its worker lease."""
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=self.settings.run_timeout)
        ).isoformat()
        with self.db.read() as connection:
            queued = [
                dict(row)
                for row in connection.execute(
                    select(t.runs).where(
                        t.runs.c.status == "queued", t.runs.c.created_at < cutoff
                    )
                ).mappings()
            ]
            stale = [
                dict(row)
                for row in connection.execute(
                    select(t.runs).where(
                        t.runs.c.status.in_(["running", "cancelling"]),
                        t.runs.c.lease_until < time.time(),
                    )
                ).mappings()
            ]
        for run in queued:
            with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
                result = connection.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == run["id"],
                        t.runs.c.status == "queued",
                        t.runs.c.created_at < cutoff,
                    )
                    .values(
                        status="expired",
                        error="排队超时，请稍后重新发起。",
                        finished_at=now(),
                    )
                )
                if result.rowcount:
                    append_event(
                        connection,
                        run["id"],
                        "run.expired",
                        {"status": "expired", "error": "排队超时，未调用模型。"},
                    )
        for run in stale:
            with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
                result = connection.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == run["id"],
                        t.runs.c.lease_until < time.time(),
                        t.runs.c.status.in_(["running", "cancelling"]),
                    )
                    .values(
                        status="failed",
                        fence=run["fence"] + 1,
                        error="执行进程失联，请核实费用后重新发起。",
                        finished_at=now(),
                    )
                )
                if result.rowcount:
                    append_event(
                        connection,
                        run["id"],
                        "run.failed",
                        {"status": "failed", "error": "执行进程失联，未自动重放。"},
                    )
        # Billing evidence is authoritative; rebuilding this projection repairs
        # a crash between financial commit and reporting update.
        with self.db.read() as connection:
            receipts = list(connection.execute(select(reservations_table)).mappings())
            run_status = dict(
                connection.execute(select(t.runs.c.id, t.runs.c.status)).all()
            )
        for row in receipts:
            if row["status"] == "reserved" and run_status.get(row["run_id"]) in (
                "failed",
                "cancelled",
                "expired",
                "succeeded",
            ):
                receipt = self.platform.billing.unresolved(
                    row["tenant_id"], row["call_id"], "执行结束但缺少结算证据。"
                )
            else:
                from agent_platform.modules.billing.service import _serialize

                receipt = _serialize(dict(row))
            sync_call_receipt(self.platform, receipt)
        with self.db.transaction("orphan-calls") as connection:
            known = [row["call_id"] for row in receipts]
            dead_runs = [
                key
                for key, value in run_status.items()
                if value in ("failed", "cancelled", "expired", "succeeded")
            ]
            if dead_runs:
                connection.execute(
                    update(t.calls)
                    .where(
                        t.calls.c.run_id.in_(dead_runs),
                        t.calls.c.status == "admitting",
                        t.calls.c.id.not_in(known),
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
                    while len(tasks) < 4:
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
