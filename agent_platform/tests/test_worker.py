"""Real worker, LangGraph, SQL transactions and billing with offline model I/O."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.platform import Platform, public_user, uid
from agent_platform.modules.runtime.gateway import (
    Gateway,
    GatewayEvent,
    ModelReply,
    UsageUnavailable,
)


def result(
    content: str = "Offline answer", calls=None, input_tokens=20, output_tokens=5
):
    return ModelReply(
        content,
        calls or [],
        input_tokens,
        output_tokens,
        {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "usage_source": "litellm_gateway",
        },
    )


class FakeGateway:
    configured = True

    def __init__(
        self,
        replies=None,
        *,
        before_result=None,
        after_result=None,
        missing=False,
        hang=False,
    ):
        self.replies = replies or [result()]
        self.calls = []
        self.before_result = before_result
        self.after_result = after_result
        self.missing = missing
        self.hang = hang
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(self, messages, model, max_tokens, temperature, tools):
        self.calls.append(
            copy.deepcopy(
                {
                    "messages": messages,
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "tools": tools,
                }
            )
        )
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        self.started.set()
        try:
            if reply.content:
                yield GatewayEvent("text", {"text": reply.content})
            if self.before_result:
                await self.before_result()
            if self.hang:
                await asyncio.Event().wait()
            if self.missing:
                raise UsageUnavailable()
            yield GatewayEvent("result", reply)
            if self.after_result:
                await self.after_result()
        finally:
            self.closed.set()


class WorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-platform-worker-")
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temp.name) / 'platform.db'}",
            admin_email="worker-admin@example.test",
            admin_password="worker-offline-password",
            embedded_worker=False,
            run_timeout=5,
        )
        self.db = Database(self.settings.database_url)
        self.db.create_schema()
        self.platform = Platform(self.db, self.settings)
        self.platform.bootstrap()
        with self.db.read() as connection:
            self.user = public_user(
                connection.execute(select(t.users)).mappings().one()
            )
        self.tenant = self.user["tenant_id"]
        self.model = self.platform.save_model(
            self.user,
            {
                "name": "Offline model",
                "alias": "offline-model",
                "active": True,
                "input_price": "1",
                "output_price": "2",
                "context_window": 32000,
                "max_output_tokens": 256,
            },
        )
        self.agent = self.platform.save_agent(
            self.user,
            {
                "name": "Offline agent",
                "description": "Worker test",
                "system_prompt": "Original published prompt",
                "model_id": self.model["id"],
                "temperature": 0,
                "max_steps": 3,
                "max_tokens": 64,
                "tools": ["calculator"],
            },
        )
        self.platform.publish(self.user, self.agent["id"])
        self.session = self.platform.create_session(self.user, self.agent["id"], None)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def credit(self) -> None:
        self.platform.billing.credit(
            self.tenant, self.user["id"], "10", "Offline test credit", uid()
        )

    def enqueue(self, message="test request", *, session=None) -> dict:
        return self.platform.enqueue(
            self.user, (session or self.session)["id"], message, uid()
        )

    def calls(self, run_id: str) -> list[dict]:
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(t.calls)
                    .where(t.calls.c.run_id == run_id)
                    .order_by(t.calls.c.created_at)
                ).mappings()
            ]

    def receipts(self, run_id: str) -> list[dict]:
        return [
            row
            for row in self.platform.billing.reservations(self.tenant)
            if row["run_id"] == run_id
        ]

    async def execute(self, gateway, *, worker=None) -> tuple[Worker, dict]:
        worker = worker or Worker(self.platform, gateway=gateway)
        run = worker.claim()
        self.assertIsNotNone(run)
        await worker.execute(run)
        return worker, run

    async def test_success_settles_once_records_events_and_preserves_history(
        self,
    ) -> None:
        self.credit()
        queued = self.enqueue("first question")
        gateway = FakeGateway()
        worker, run = await self.execute(gateway)
        self.assertEqual(run["id"], queued["id"])
        saved = self.platform.get_run(self.user, run["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(Decimal(saved["cost"]), Decimal("0.00003"))
        wallet = self.platform.billing.wallet(self.tenant)
        self.assertEqual(Decimal(wallet["balance"]), Decimal("9.99997"))
        self.assertEqual(Decimal(wallet["reserved"]), 0)
        self.assertEqual(self.receipts(run["id"])[0]["status"], "settled")
        self.assertEqual(
            self.calls(run["id"])[0]["raw_usage"]["usage_source"], "litellm_gateway"
        )
        events = self.platform.events(self.user, run["id"], 0)
        self.assertEqual(
            [event["sequence"] for event in events], list(range(1, len(events) + 1))
        )
        self.assertEqual(events[0]["type"], "run.queued")
        self.assertEqual(events[-1]["type"], "run.completed")
        self.assertEqual(
            "".join(
                event["data"]["text"]
                for event in events
                if event["type"] == "message.delta"
            ),
            "Offline answer",
        )
        worker.recover()
        worker.recover()
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["balance"]),
            Decimal("9.99997"),
        )
        self.enqueue("second question")
        await self.execute(gateway, worker=worker)
        messages = gateway.calls[1]["messages"]
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(messages[-2]["content"], "Offline answer")
        detail = self.platform.session_detail(self.user, self.session["id"])
        self.assertEqual(len(detail["messages"]), 4)

    async def test_tool_two_steps_each_admitted_and_settled(self) -> None:
        self.credit()
        self.enqueue()
        gateway = FakeGateway(
            [
                result(
                    "",
                    [
                        {
                            "id": "calc1",
                            "name": "calculator",
                            "args": {"expression": "2 + 2"},
                        }
                    ],
                    10,
                    4,
                ),
                result("Answer: 4", input_tokens=20, output_tokens=6),
            ]
        )
        _, run = await self.execute(gateway)
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.calls[1]["messages"][-1]["role"], "tool")
        self.assertIn('"result": 4', gateway.calls[1]["messages"][-1]["content"])
        self.assertEqual(
            [row["status"] for row in self.receipts(run["id"])], ["settled", "settled"]
        )
        self.assertEqual(
            Decimal(self.platform.get_run(self.user, run["id"])["cost"]),
            Decimal("0.00005"),
        )
        self.assertEqual(
            sum(
                e["type"] == "tool.finished"
                for e in self.platform.events(self.user, run["id"], 0)
            ),
            1,
        )

    async def test_zero_balance_rejects_without_calling_gateway(self) -> None:
        self.enqueue()
        gateway = FakeGateway()
        _, run = await self.execute(gateway)
        self.assertEqual(gateway.calls, [])
        self.assertEqual(self.receipts(run["id"]), [])
        self.assertEqual(self.calls(run["id"])[0]["status"], "rejected")
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "failed"
        )
        self.assertEqual(self.platform.billing.ledger(self.tenant), [])

    async def test_missing_usage_retains_reservation_and_no_assistant_history(
        self,
    ) -> None:
        self.credit()
        self.enqueue()
        _, run = await self.execute(FakeGateway(missing=True))
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "failed"
        )
        self.assertEqual(self.receipts(run["id"])[0]["status"], "unresolved")
        wallet = self.platform.billing.wallet(self.tenant)
        self.assertEqual(Decimal(wallet["balance"]), 10)
        self.assertGreater(Decimal(wallet["reserved"]), 0)
        self.assertEqual(
            [
                message["role"]
                for message in self.platform.session_detail(
                    self.user, self.session["id"]
                )["messages"]
            ],
            ["user"],
        )

    async def test_cancel_queued_run_never_claimed(self) -> None:
        queued = self.enqueue()
        self.platform.cancel_run(self.user, queued["id"])
        gateway = FakeGateway()
        self.assertIsNone(Worker(self.platform, gateway=gateway).claim())
        self.assertEqual(gateway.calls, [])

    async def test_cancel_inflight_keeps_unknown_fee_frozen(self) -> None:
        self.credit()
        queued = self.enqueue()
        gateway = FakeGateway(hang=True)
        worker = Worker(self.platform, gateway=gateway)
        task = asyncio.create_task(worker.execute(worker.claim()))
        await asyncio.wait_for(gateway.started.wait(), 2)
        self.platform.cancel_run(self.user, queued["id"])
        await asyncio.wait_for(task, 2)
        self.assertTrue(gateway.closed.is_set())
        self.assertEqual(
            self.platform.get_run(self.user, queued["id"])["status"], "cancelled"
        )
        self.assertEqual(self.receipts(queued["id"])[0]["status"], "unresolved")
        self.assertGreater(
            Decimal(self.platform.billing.wallet(self.tenant)["reserved"]), 0
        )

    async def test_timeout_closes_model_and_preserves_unknown_fee(self) -> None:
        self.credit()
        self.enqueue()
        gateway = FakeGateway(hang=True)
        platform = Platform(self.db, replace(self.settings, run_timeout=0.1))
        worker, run = await self.execute(
            gateway, worker=Worker(platform, gateway=gateway)
        )
        self.assertTrue(gateway.closed.is_set())
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "expired"
        )
        self.assertEqual(self.receipts(run["id"])[0]["status"], "unresolved")
        self.assertIsNone(worker.claim())

    async def test_expired_queue_is_closed_without_gateway_or_charge(self) -> None:
        queued = self.enqueue()
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == queued["id"])
                .values(
                    created_at=(datetime.now(UTC) - timedelta(seconds=10)).isoformat()
                )
            )
        gateway = FakeGateway()
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        self.assertIsNone(worker.claim())
        self.assertEqual(
            self.platform.get_run(self.user, queued["id"])["status"], "expired"
        )
        self.assertEqual(gateway.calls, [])
        self.assertEqual(self.receipts(queued["id"]), [])

    async def test_configured_run_cost_cap_blocks_before_gateway(self) -> None:
        self.credit()
        queued = self.enqueue()
        gateway = FakeGateway()
        limited = Platform(self.db, replace(self.settings, max_run_cost="0.000001"))
        await self.execute(gateway, worker=Worker(limited, gateway=gateway))
        self.assertEqual(gateway.calls, [])
        self.assertEqual(
            self.platform.get_run(self.user, queued["id"])["status"], "failed"
        )
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["reserved"]), Decimal(0)
        )

    async def test_gateway_configuration_failure_releases_unsent_reservation(
        self,
    ) -> None:
        self.credit()
        self.enqueue()
        _, run = await self.execute(Gateway("", ""))
        self.assertEqual(self.receipts(run["id"])[0]["status"], "released")
        wallet = self.platform.billing.wallet(self.tenant)
        self.assertEqual(Decimal(wallet["balance"]), 10)
        self.assertEqual(Decimal(wallet["reserved"]), 0)

    async def test_late_cancel_preserves_already_received_usage(self) -> None:
        self.credit()
        queued = self.enqueue()

        async def after_result():
            self.platform.cancel_run(self.user, queued["id"])
            await asyncio.Event().wait()

        gateway = FakeGateway(after_result=after_result)
        _, run = await asyncio.wait_for(self.execute(gateway), 2)
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "cancelled"
        )
        self.assertEqual(self.receipts(run["id"])[0]["status"], "settled")
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["balance"]),
            Decimal("9.99997"),
        )
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["reserved"]), 0
        )

    async def test_lost_lease_recovery_never_replays_unknown_call(self) -> None:
        self.credit()
        queued = self.enqueue()
        gateway = FakeGateway(hang=True)
        worker = Worker(self.platform, gateway=gateway)
        task = asyncio.create_task(worker.execute(worker.claim()))
        await asyncio.wait_for(gateway.started.wait(), 2)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == queued["id"])
                .values(lease_until=time.time() - 1)
            )
        replacement = Worker(self.platform, gateway=FakeGateway())
        replacement.recover()
        await asyncio.wait_for(task, 2)
        self.assertEqual(
            self.platform.get_run(self.user, queued["id"])["status"], "failed"
        )
        self.assertEqual(self.receipts(queued["id"])[0]["status"], "unresolved")
        self.assertIsNone(replacement.claim())
        self.assertEqual(len(gateway.calls), 1)

    async def test_known_usage_is_settled_even_after_lease_loss(self) -> None:
        self.credit()
        queued = self.enqueue()

        async def after_result():
            with self.db.transaction(f"tenant:{self.tenant}") as connection:
                connection.execute(
                    update(t.runs)
                    .where(t.runs.c.id == queued["id"])
                    .values(lease_until=time.time() - 1)
                )

        gateway = FakeGateway(after_result=after_result)
        worker, run = await self.execute(gateway)
        worker.recover()
        self.assertEqual(self.receipts(run["id"])[0]["status"], "settled")
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["reserved"]), 0
        )
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["balance"]),
            Decimal("9.99997"),
        )
        self.assertIsNone(worker.claim())

    async def test_model_disable_and_output_reduction_rechecked_before_admission(
        self,
    ) -> None:
        self.credit()
        for change in [{"active": False}, {"active": True, "max_output_tokens": 32}]:
            with self.subTest(change=change):
                self.platform.save_model(
                    self.user,
                    {"active": True, "max_output_tokens": 256},
                    self.model["id"],
                )
                self.enqueue()
                self.platform.save_model(self.user, change, self.model["id"])
                gateway = FakeGateway()
                _, run = await self.execute(gateway)
                self.assertEqual(gateway.calls, [])
                self.assertEqual(self.receipts(run["id"]), [])
                self.assertEqual(self.calls(run["id"])[0]["status"], "rejected")

    async def test_quota_change_between_tool_steps_blocks_only_next_call(self) -> None:
        self.credit()
        self.enqueue()

        async def after_result():
            self.platform.billing.set_quota(
                self.tenant, "model", self.model["id"], 0, None, None, None
            )

        gateway = FakeGateway(
            [
                result(
                    "",
                    [
                        {
                            "id": "calc",
                            "name": "calculator",
                            "args": {"expression": "1+1"},
                        }
                    ],
                )
            ],
            after_result=after_result,
        )
        _, run = await self.execute(gateway)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "failed"
        )
        self.assertEqual(
            [row["status"] for row in self.calls(run["id"])], ["confirmed", "rejected"]
        )
        self.assertEqual(self.receipts(run["id"])[0]["status"], "settled")

    async def test_price_change_does_not_reprice_enqueued_run(self) -> None:
        self.credit()
        self.enqueue()
        self.platform.save_model(
            self.user, {"input_price": "100", "output_price": "200"}, self.model["id"]
        )
        _, run = await self.execute(FakeGateway())
        self.assertEqual(
            Decimal(self.platform.get_run(self.user, run["id"])["cost"]),
            Decimal("0.00003"),
        )
        self.assertEqual(self.receipts(run["id"])[0]["input_price"], "1")

    async def test_recovery_restores_stale_call_projection_without_double_charge(
        self,
    ) -> None:
        self.credit()
        self.enqueue()
        worker, run = await self.execute(FakeGateway())
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.calls)
                .where(t.calls.c.run_id == run["id"])
                .values(status="running", cost="0", input_tokens=0, output_tokens=0)
            )
        worker.recover()
        worker.recover()
        self.assertEqual(self.calls(run["id"])[0]["status"], "confirmed")
        self.assertEqual(Decimal(self.calls(run["id"])[0]["cost"]), Decimal("0.00003"))
        self.assertEqual(
            Decimal(self.platform.billing.wallet(self.tenant)["balance"]),
            Decimal("9.99997"),
        )


if __name__ == "__main__":
    unittest.main()
