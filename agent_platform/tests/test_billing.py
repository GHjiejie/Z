"""Offline tests of money, concurrency and all-or-nothing admission invariants."""

import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from agent_platform.infrastructure.db import Database, metadata
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import (
    BillingService,
    RedisLimiter,
    billing_audit,
    ledger_entries,
    reconciliations,
    reservations_table,
)


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(f"sqlite:///{Path(self.temporary.name) / 'billing.db'}")
        metadata.create_all(self.db.engine)
        self.billing = BillingService(self.db)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def credit(self, amount="10", tenant="tenant-a", key="credit-1"):
        return self.billing.credit(
            tenant, "admin-a", amount, "Manual credit receipt", key
        )

    def reserve(
        self,
        call="call-1",
        tenant="tenant-a",
        user="user-a",
        model="model-a",
        input_tokens=100,
        output_tokens=100,
        price="1000",
    ):
        return self.billing.reserve(
            tenant,
            user,
            model,
            "run-a",
            call,
            input_tokens,
            output_tokens,
            price,
            price,
        )

    def quota(
        self,
        scope="tenant",
        subject="tenant-a",
        rpm=60,
        tpm=120000,
        concurrent=4,
        budget=None,
    ):
        return self.billing.set_quota(
            "tenant-a", scope, subject, rpm, tpm, concurrent, budget
        )

    def error(self, code, operation):
        with self.assertRaises(PlatformError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_wallet_starts_at_zero_and_requires_explicit_credit(self):
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["balance"]), 0)
        self.error("insufficient_balance", self.reserve)
        self.assertEqual(self.billing.reservations("tenant-a"), [])
        self.assertEqual(self.billing.ledger("tenant-a"), [])

    def test_credit_idempotency_binds_actor_amount_and_description(self):
        self.credit()
        self.credit()
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["balance"]), 10)
        self.error("idempotency_conflict", lambda: self.credit("11"))
        self.error(
            "idempotency_conflict",
            lambda: self.billing.credit(
                "tenant-a", "another-admin", "10", "Manual credit receipt", "credit-1"
            ),
        )
        self.error(
            "idempotency_conflict",
            lambda: self.billing.credit(
                "tenant-a", "admin-a", "10", "Changed receipt", "credit-1"
            ),
        )
        with self.db.read() as conn:
            audits = list(conn.execute(select(billing_audit)).mappings())
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["actor_id"], "admin-a")

    def test_money_remains_exact_below_float_precision(self):
        self.credit("999999999.123456789012")
        self.credit("0.000000000001", key="tiny-credit")
        self.assertEqual(
            self.billing.wallet("tenant-a")["balance"], "999999999.123456789013"
        )

    def test_concurrent_requests_cannot_spend_the_same_balance(self):
        self.credit("0.2")
        barrier = Barrier(2)

        def attempt(call):
            barrier.wait()
            try:
                return self.reserve(call)["status"]
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, ["one", "two"]))
        self.assertCountEqual(outcomes, ["reserved", "insufficient_balance"])
        wallet = self.billing.wallet("tenant-a")
        self.assertEqual(Decimal(wallet["reserved"]), Decimal("0.2"))
        self.assertEqual(Decimal(wallet["available"]), 0)
        self.assertEqual(len(self.billing.reservations("tenant-a")), 1)

    def test_duplicate_reservation_and_settlement_never_double_charge(self):
        self.credit()
        first = self.reserve()
        self.assertEqual(self.reserve()["id"], first["id"])
        self.error("idempotency_conflict", lambda: self.reserve(output_tokens=99))
        first = self.billing.settle("tenant-a", "call-1", 100, 25, "0.001")
        second = self.billing.settle("tenant-a", "call-1", 100, 25, "0.001")
        self.assertEqual(first, second)
        self.assertEqual(
            Decimal(self.billing.wallet("tenant-a")["balance"]), Decimal("9.875")
        )
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        self.assertEqual(len(self.billing.ledger("tenant-a")), 2)
        self.error(
            "settlement_conflict",
            lambda: self.billing.settle("tenant-a", "call-1", 100, 26),
        )

    def test_postings_balance_and_wallet_projection_can_be_rebuilt(self):
        self.credit()
        self.reserve()
        self.billing.settle("tenant-a", "call-1", 100, 50)
        with self.db.read() as conn:
            entries = list(conn.execute(select(ledger_entries)).mappings())
        transactions = {}
        for entry in entries:
            transactions.setdefault(entry["transaction_id"], Decimal(0))
            transactions[entry["transaction_id"]] += entry["amount"]
        self.assertTrue(all(total == 0 for total in transactions.values()))
        projected = sum(
            (e["amount"] for e in entries if e["account"] == "customer_wallet"),
            Decimal(0),
        )
        self.assertEqual(projected, Decimal(self.billing.wallet("tenant-a")["balance"]))

    def test_tenant_user_and_model_quotas_all_apply_without_partial_admission(self):
        self.credit()
        self.quota(scope="user", subject="user-a", rpm=1)
        self.quota(scope="model", subject="model-a", tpm=199)
        self.error("tpm_limit", self.reserve)
        self.assertEqual(self.billing.reservations("tenant-a"), [])
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        self.quota(scope="model", subject="model-a", tpm=200)
        self.reserve()
        self.error("rpm_limit", lambda: self.reserve("call-2", model="model-b"))
        self.error("tpm_limit", lambda: self.reserve("call-3", user="user-b"))
        self.assertEqual(len(self.billing.reservations("tenant-a")), 1)

    def test_zero_limits_deny_even_zero_priced_calls(self):
        for field in ("rpm", "tpm", "concurrent", "budget"):
            with self.subTest(field=field):
                self.quota(**{field: 0})
                expected = "budget_exceeded" if field == "budget" else f"{field}_limit"
                self.error(
                    expected,
                    lambda: self.reserve(price="0", input_tokens=0, output_tokens=0),
                )
        self.assertEqual(self.billing.reservations("tenant-a"), [])

    def test_user_and_model_budgets_are_cumulative_and_count_inflight_money(self):
        self.credit()
        self.quota(scope="user", subject="user-a", budget="0.3")
        self.quota(scope="model", subject="model-a", budget="0.2")
        self.reserve()
        self.error(
            "budget_exceeded", lambda: self.reserve("same-user", model="model-b")
        )
        self.error("budget_exceeded", lambda: self.reserve("same-model", user="user-b"))
        self.billing.settle("tenant-a", "call-1", 100, 0)
        self.reserve("fits-budget", input_tokens=50, output_tokens=50)
        self.error(
            "budget_exceeded",
            lambda: self.reserve("over-budget", input_tokens=1, output_tokens=0),
        )

    def test_unknown_usage_keeps_money_and_capacity_after_rate_window(self):
        self.credit()
        self.quota(concurrent=1)
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Gateway disconnected")
        self.billing.unresolved("tenant-a", "call-1", "Duplicate notification")
        old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        with self.db.transaction("tenant:tenant-a") as conn:
            conn.execute(reservations_table.update().values(created_at=old))
        self.error("concurrent_limit", lambda: self.reserve("next"))
        self.error(
            "unresolved_call",
            lambda: self.billing.release("tenant-a", "call-1", "TTL expired"),
        )
        self.assertEqual(
            Decimal(self.billing.wallet("tenant-a")["reserved"]), Decimal("0.2")
        )
        self.billing.settle("tenant-a", "call-1", 100, 10)
        self.reserve("reconciled")

    def test_token_adjustment_releases_unused_tokens_in_original_window(self):
        self.credit()
        self.quota(tpm=250)
        self.reserve()
        self.error("tpm_limit", lambda: self.reserve("next"))
        self.billing.settle("tenant-a", "call-1", 50, 0)
        self.reserve("next")

    def test_release_is_idempotent_and_returns_all_unsent_capacity(self):
        self.credit()
        self.quota(rpm=1, tpm=200, concurrent=1)
        self.reserve()
        self.billing.release("tenant-a", "call-1", "Permission revoked before send")
        self.billing.release("tenant-a", "call-1", "Repeated")
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        self.reserve("next")
        self.error(
            "reservation_released",
            lambda: self.billing.settle("tenant-a", "call-1", 100, 50),
        )

    def test_invalid_usage_becomes_unresolved_without_refunding(self):
        self.credit()
        for i, value in enumerate((-1, True, 1.5, None, 2_000_000_001)):
            call = f"bad-{i}"
            # Disable concurrency limit only for this validation test.
            self.quota(concurrent=None)
            self.reserve(call)
            self.error(
                "invalid_usage",
                lambda call=call, value=value: self.billing.settle(
                    "tenant-a", call, value, 10
                ),
            )
        self.assertTrue(
            all(
                r["status"] == "unresolved"
                for r in self.billing.reservations("tenant-a")
            )
        )
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 1)

    def test_cached_and_reasoning_details_do_not_duplicate_token_charges(self):
        self.credit()
        self.reserve()
        result = self.billing.settle(
            "tenant-a",
            "call-1",
            100,
            40,
            raw_usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 30},
            },
        )
        self.assertEqual(Decimal(result["cost"]), Decimal("0.14"))
        self.assertEqual(
            result["raw_usage"]["completion_tokens_details"]["reasoning_tokens"], 30
        )

    def test_actual_cost_over_reservation_never_overdraws_wallet(self):
        self.credit("0.2")
        self.reserve()
        result = self.billing.settle("tenant-a", "call-1", 200, 200)
        self.assertEqual(Decimal(result["cost"]), Decimal("0.2"))
        self.assertEqual(Decimal(result["overage"]), Decimal("0.2"))
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["balance"]), 0)
        self.assertTrue(self.billing.wallet("tenant-a")["blocked"])
        self.error("billing_review_required", lambda: self.reserve("next", price="0"))

    def test_tenant_isolation_covers_money_receipts_and_idempotency(self):
        self.credit()
        self.credit("20", tenant="tenant-b")
        self.reserve()
        self.reserve(tenant="tenant-b")
        self.billing.settle("tenant-b", "call-1", 10, 10)
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["balance"]), 10)
        self.assertEqual(self.billing.reservations("tenant-a")[0]["status"], "reserved")
        self.error(
            "reservation_not_found",
            lambda: self.billing.settle("tenant-c", "call-1", 10, 10),
        )
        self.assertEqual(self.billing.ledger("tenant-c"), [])

    def test_redis_failure_fails_closed_and_does_not_hold_wallet(self):
        self.credit()
        limiter = Mock()
        limiter.admit.side_effect = PlatformError(
            503, "rate_limiter_unavailable", "Offline"
        )
        self.billing._limiter = limiter
        self.error("rate_limiter_unavailable", self.reserve)
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        self.assertEqual(self.billing.reservations("tenant-a"), [])
        limiter.finish.assert_called_once()

    def test_database_commit_failure_compensates_redis_admission(self):
        self.credit()
        limiter = Mock()
        self.billing._limiter = limiter
        original_transaction = self.db.transaction

        @contextmanager
        def fail_commit(scope):
            with original_transaction(scope) as conn:
                yield conn
                raise RuntimeError("Simulated commit failure")

        self.db.transaction = fail_commit
        with self.assertRaisesRegex(RuntimeError, "commit failure"):
            self.reserve()
        self.db.transaction = original_transaction
        self.assertEqual(self.billing.reservations("tenant-a"), [])
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        limiter.admit.assert_called_once()
        limiter.finish.assert_called_once_with(
            "tenant-a", "call-1", ["tenant:tenant-a"], 0, rollback=True
        )

    def test_every_mutation_rechecks_authority_inside_the_tenant_transaction(self):
        self.credit()
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Missing receipt")
        before = self.billing.wallet("tenant-a")
        checks = []

        def denied(conn):
            self.assertTrue(conn.in_transaction())
            checks.append(conn.execute(select(reservations_table)).first())
            raise PlatformError(403, "permission_revoked", "Role changed")

        protected = BillingService(self.db, authorize=denied)
        operations = [
            lambda: protected.credit("tenant-a", "admin", "1", "New credit", "new"),
            lambda: protected.set_quota(
                "tenant-a", "tenant", "tenant-a", None, None, None, None
            ),
            lambda: protected.reserve(
                "tenant-a", "user", "model", "run", "new", 1, 1, "1", "1"
            ),
            lambda: protected.settle("tenant-a", "call-1", 100, 10),
            lambda: protected.release("tenant-a", "call-1", "Do not release"),
            lambda: protected.unresolved("tenant-a", "call-1", "Do not update"),
            lambda: protected.resolve(
                "tenant-a",
                "call-1",
                action="write_off",
                reason="Do not waive",
                actor_id="admin",
                idempotency_key="reconcile",
            ),
            lambda: protected.unblock("tenant-a", "admin", "Do not unblock"),
        ]
        for operation in operations:
            self.error("permission_revoked", operation)
        self.assertEqual(len(checks), len(operations))
        self.assertEqual(before, self.billing.wallet("tenant-a"))
        self.assertEqual(
            self.billing.reservations("tenant-a")[0]["status"], "unresolved"
        )
        self.assertEqual(len(self.billing.ledger("tenant-a")), 1)

    def test_manual_confirmation_is_audited_and_binds_idempotency_content(self):
        self.credit()
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Missing final usage")
        arguments = {
            "input_tokens": 100,
            "output_tokens": 25,
            "action": "confirm",
            "reason": "Verified gateway report #42",
            "actor_id": "finance-admin",
            "idempotency_key": "resolution-1",
        }
        result = self.billing.resolve("tenant-a", "call-1", **arguments)
        self.assertEqual(
            self.billing.resolve("tenant-a", "call-1", **arguments), result
        )
        self.assertEqual(result["raw_usage"]["source"], "manual_reconciliation")
        self.assertEqual(result["raw_usage"]["reason"], arguments["reason"])
        self.assertEqual(Decimal(result["cost"]), Decimal("0.125"))
        self.assertEqual(len(self.billing.ledger("tenant-a")), 2)
        arguments["output_tokens"] = 30
        self.error(
            "idempotency_conflict",
            lambda: self.billing.resolve("tenant-a", "call-1", **arguments),
        )
        with self.db.read() as conn:
            audits = list(
                conn.execute(
                    select(billing_audit).where(
                        billing_audit.c.action == "billing.resolve.confirm"
                    )
                ).mappings()
            )
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["actor_id"], "finance-admin")

    def test_manual_write_off_releases_money_without_erasing_usage_or_creating_a_refund(
        self,
    ):
        self.credit()
        self.quota(tpm=200, concurrent=1)
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Provider cannot supply usage")
        arguments = {
            "action": "write_off",
            "reason": "Waived after provider investigation #19",
            "actor_id": "finance-admin",
            "idempotency_key": "write-off-1",
        }
        result = self.billing.resolve("tenant-a", "call-1", **arguments)
        self.assertEqual(
            self.billing.resolve("tenant-a", "call-1", **arguments), result
        )
        self.assertEqual(result["status"], "written_off")
        self.assertEqual(result["rate_tokens"], 200)
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["balance"]), 10)
        self.assertEqual(Decimal(self.billing.wallet("tenant-a")["reserved"]), 0)
        self.assertEqual(len(self.billing.ledger("tenant-a")), 1)
        self.error("tpm_limit", lambda: self.reserve("next"))
        self.error(
            "reservation_released",
            lambda: self.billing.settle("tenant-a", "call-1", 100, 100),
        )
        self.quota(tpm=400, concurrent=1)
        self.reserve("next")

    def test_manual_resolution_rejects_wrong_tenant_incomplete_evidence_and_final_calls(
        self,
    ):
        self.credit()
        self.reserve()
        arguments = {
            "action": "write_off",
            "reason": "Investigation",
            "actor_id": "admin",
            "idempotency_key": "resolution",
        }
        self.error(
            "resolution_not_pending",
            lambda: self.billing.resolve("tenant-a", "call-1", **arguments),
        )
        self.error(
            "reservation_not_found",
            lambda: self.billing.resolve("tenant-b", "call-1", **arguments),
        )
        self.billing.unresolved("tenant-a", "call-1", "Provider lost receipt")
        self.error(
            "invalid_usage",
            lambda: self.billing.resolve(
                "tenant-a",
                "call-1",
                action="confirm",
                reason="Missing tokens",
                actor_id="admin",
                idempotency_key="confirm",
            ),
        )
        self.error(
            "invalid_resolution",
            lambda: self.billing.resolve(
                "tenant-a", "call-1", input_tokens=0, **arguments
            ),
        )
        self.error(
            "audit_required",
            lambda: self.billing.resolve(
                "tenant-a",
                "call-1",
                action="write_off",
                reason=" ",
                actor_id="admin",
                idempotency_key="confirm",
            ),
        )
        self.assertEqual(
            self.billing.reservations("tenant-a")[0]["status"], "unresolved"
        )

    def test_concurrent_manual_confirmation_cannot_charge_twice(self):
        self.credit()
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Missing final usage")
        barrier = Barrier(2)

        def resolve(_index):
            barrier.wait()
            return self.billing.resolve(
                "tenant-a",
                "call-1",
                100,
                50,
                action="confirm",
                reason="Report verified",
                actor_id="admin",
                idempotency_key="shared-key",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(resolve, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            Decimal(self.billing.wallet("tenant-a")["balance"]), Decimal("9.85")
        )
        self.assertEqual(len(self.billing.ledger("tenant-a")), 2)

    def test_manual_reconciliation_and_audit_share_one_atomic_transaction(self):
        self.credit()
        self.reserve()
        self.billing.unresolved("tenant-a", "call-1", "Missing final usage")
        self.billing._audit = Mock(side_effect=RuntimeError("Audit storage failed"))
        with self.assertRaisesRegex(RuntimeError, "Audit storage failed"):
            self.billing.resolve(
                "tenant-a",
                "call-1",
                100,
                50,
                action="confirm",
                reason="Report verified",
                actor_id="admin",
                idempotency_key="attempt",
            )
        self.assertEqual(
            self.billing.reservations("tenant-a")[0]["status"], "unresolved"
        )
        self.assertEqual(
            Decimal(self.billing.wallet("tenant-a")["reserved"]), Decimal("0.2")
        )
        self.assertEqual(len(self.billing.ledger("tenant-a")), 1)
        with self.db.read() as conn:
            self.assertEqual(list(conn.execute(select(reconciliations))), [])

    def test_unblock_requires_resolution_and_records_review_without_erasing_overage(
        self,
    ):
        self.credit()
        self.reserve()
        self.reserve("unknown")
        self.billing.unresolved("tenant-a", "unknown", "Incomplete usage")
        self.billing.settle("tenant-a", "call-1", 200, 200)
        self.error(
            "unresolved_billing",
            lambda: self.billing.unblock("tenant-a", "admin", "Review"),
        )
        self.billing.resolve(
            "tenant-a",
            "unknown",
            action="write_off",
            reason="Waiver approved",
            actor_id="admin",
            idempotency_key="waiver",
        )
        self.billing.unblock(
            "tenant-a", "admin", "Accepted excess cost; pricing corrected"
        )
        self.assertFalse(self.billing.wallet("tenant-a")["blocked"])
        settled = next(
            row
            for row in self.billing.reservations("tenant-a")
            if row["call_id"] == "call-1"
        )
        self.assertEqual(Decimal(settled["overage"]), Decimal("0.2"))
        self.assertEqual(len(self.billing.ledger("tenant-a")), 2)
        with self.db.read() as conn:
            audit = (
                conn.execute(
                    select(billing_audit).where(
                        billing_audit.c.action == "billing.unblock"
                    )
                )
                .mappings()
                .one()
            )
        self.assertIn("Accepted excess cost", audit["details"])
        self.assertIn("call-1", audit["details"])
        self.reserve("resumed")


class RedisBillingTest(unittest.TestCase):
    """Exercise real Lua atomics when the local redis-server executable exists."""

    @classmethod
    def setUpClass(cls):
        executable = shutil.which("redis-server")
        if not executable:
            raise unittest.SkipTest("redis-server is not installed")
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="platform-redis-", dir="/tmp"
        )
        socket_path = str(Path(cls.temporary.name) / "redis.sock")
        cls.url = f"unix://{socket_path}"
        cls.process = subprocess.Popen(
            [
                executable,
                "--port",
                "0",
                "--unixsocket",
                socket_path,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                cls.temporary.name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.limiter = RedisLimiter(cls.url)
        for _ in range(100):
            try:
                cls.limiter.client.ping()
                return
            except RedisConnectionError:
                time.sleep(0.02)
        cls.tearDownClass()
        raise RuntimeError("Local isolated Redis did not start")

    @classmethod
    def tearDownClass(cls):
        cls.limiter.client.close()
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.tenant = uuid.uuid4().hex

    def policy(self, scope="tenant", subject=None, rpm=None, tpm=None, concurrent=None):
        return {
            "scope": scope,
            "subject_id": subject or self.tenant,
            "rpm": rpm,
            "tpm": tpm,
            "concurrent": concurrent,
        }

    def test_lua_rejection_does_not_partially_consume_other_dimensions(self):
        policies = [self.policy(rpm=2), self.policy("user", "user-a", rpm=0)]
        with self.assertRaises(PlatformError):
            self.limiter.admit(self.tenant, "denied", 100, policies)
        tenant_keys = self.limiter._keys(self.tenant, [f"tenant:{self.tenant}"])
        self.assertEqual(self.limiter.client.zcard(tenant_keys[0]), 0)
        policies[1]["rpm"] = 1
        self.limiter.admit(self.tenant, "accepted", 100, policies)
        with self.assertRaises(PlatformError):
            self.limiter.admit(self.tenant, "denied-again", 100, policies)
        self.assertEqual(self.limiter.client.zcard(tenant_keys[0]), 1)

    def test_lua_concurrent_admissions_have_an_atomic_capacity_bound(self):
        barrier = Barrier(8)
        policies = [self.policy(concurrent=2)]

        def attempt(index):
            barrier.wait()
            try:
                self.limiter.admit(self.tenant, f"call-{index}", 100, policies)
                return "accepted"
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertEqual(results.count("accepted"), 2)
        self.assertEqual(results.count("concurrent_limit"), 6)

    def test_late_usage_does_not_refund_tokens_to_a_new_window(self):
        policy = self.policy(tpm=100)
        scopes = [f"tenant:{self.tenant}"]
        keys = self.limiter._keys(self.tenant, scopes)
        self.limiter.admit(self.tenant, "old", 100, [policy])
        self.limiter.client.zadd(keys[0], {"old": time.time() - 70})
        self.limiter.admit(self.tenant, "current", 100, [policy])
        self.limiter.finish(self.tenant, "old", scopes, 0)
        self.assertEqual(self.limiter.client.hgetall(keys[1]), {"current": "100"})
        with self.assertRaises(PlatformError) as caught:
            self.limiter.admit(self.tenant, "excess", 1, [policy])
        self.assertEqual(caught.exception.code, "tpm_limit")

    def test_expired_redis_lease_cannot_release_unknown_database_capacity(self):
        db = Database(f"sqlite:///{Path(self.temporary.name) / (self.tenant + '.db')}")
        metadata.create_all(db.engine)
        billing = BillingService(db, redis_url=self.url)
        try:
            billing.credit(self.tenant, "admin", "1", "Test funds", "credit")
            billing.set_quota(self.tenant, "tenant", self.tenant, None, None, 1, None)
            billing.reserve(
                self.tenant, "user", "model", "run", "old", 100, 100, "1", "1"
            )
            billing.unresolved(self.tenant, "old", "Unknown remote state")
            scopes = [f"tenant:{self.tenant}"]
            keys = self.limiter._keys(self.tenant, scopes)
            self.limiter.client.delete(*keys)
            with self.assertRaises(PlatformError) as caught:
                billing.reserve(
                    self.tenant, "user", "model", "run", "new", 100, 100, "1", "1"
                )
            self.assertEqual(caught.exception.code, "concurrent_limit")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
