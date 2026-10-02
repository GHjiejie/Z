"""Fresh, persisted fixtures for resource UI acceptance against the real API."""

import argparse
import hashlib
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, update

from agent_platform.apps.api.main import create_app
from agent_platform.infrastructure import gateway_tables as g
from agent_platform.infrastructure import operations_tables as ot
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.modules.billing.service import (
    ledger_transactions,
    reservations_table,
    wallets,
)
from agent_platform.modules.platform import hasher, now

parser = argparse.ArgumentParser()
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--port", type=int, default=8035)
parser.add_argument("--extra-calls", type=int, default=0)
parser.add_argument(
    "--reference-expiry",
    action="store_true",
    help="Use the Figma example expiry for screenshot comparison only",
)
args = parser.parse_args()
run = args.directory.resolve()
run.mkdir(parents=True, exist_ok=False)
os.environ["PLATFORM_DATABASE_URL"] = f"sqlite:///{run / 'acceptance.db'}"
command.upgrade(Config("agent_platform/alembic.ini"), "head")
settings = Settings(
    database_url=os.environ["PLATFORM_DATABASE_URL"],
    admin_email="admin@example.test",
    admin_password="Agent-ui-acceptance-2026",
    default_model="gpt-4o",
    default_model_input_price="5",
    default_model_output_price="15",
    web_directory=run / "unused-web",
    export_directory=run / "exports",
    tombstone_path=run / "tombstones.jsonl",
)
app = create_app(settings)
p, db = app.state.platform, app.state.database
p.bootstrap()
with db.transaction("resource-acceptance-fixtures") as c:
    owner = dict(
        c.execute(select(t.users).where(t.users.c.email == settings.admin_email))
        .mappings()
        .one()
    )
    tenant = owner["tenant_id"]
    model = c.scalar(select(t.models.c.id).where(t.models.c.alias == "gpt-4o"))
    c.execute(update(t.tenants).where(t.tenants.c.id == tenant).values(name="工作空间"))
    c.execute(
        update(t.models)
        .where(t.models.c.id == model)
        .values(
            name="OpenAI GPT-4o",
            input_price="5",
            output_price="15",
            context_window=128000,
            max_output_tokens=4096,
        )
    )
    second = "model_claude_acceptance"
    c.execute(
        insert(t.models).values(
            id=second,
            tenant_id=tenant,
            name="Anthropic Claude 3.5 Sonnet",
            alias="claude-3-5-sonnet",
            active=True,
            input_price="3",
            output_price="15",
            context_window=200000,
            max_output_tokens=8192,
            price_version=1,
            created_at="2026-09-30T00:00:00Z",
        )
    )
    existing = (
        c.execute(select(g.model_bindings).where(g.model_bindings.c.model_id == model))
        .mappings()
        .first()
    )
    if existing is None:
        c.execute(
            insert(g.deployments).values(
                id="resource_deployment",
                name="验收夹具",
                owner_scope="platform",
                gateway_id="primary",
                internal_route="acceptance-only",
                base_url="http://127.0.0.1:4000",
                protocol="openai",
                capabilities=["chat"],
                status="active",
                config_version=1,
                created_at=now(),
            )
        )
        c.execute(
            insert(g.prices).values(
                id="price_gpt_acceptance",
                tenant_id=tenant,
                model_id=model,
                version=1,
                currency="USD",
                unit="million_tokens",
                input_price=Decimal(5),
                output_price=Decimal(15),
                created_by=owner["id"],
                created_at=now(),
            )
        )
        c.execute(
            insert(g.model_bindings).values(
                tenant_id=tenant,
                model_id=model,
                deployment_id="resource_deployment",
                price_version_id="price_gpt_acceptance",
                enabled=True,
                policy_version=1,
                created_at=now(),
            )
        )
    binding = dict(
        c.execute(select(g.model_bindings).where(g.model_bindings.c.model_id == model))
        .mappings()
        .one()
    )
    price = dict(
        c.execute(select(g.prices).where(g.prices.c.id == binding["price_version_id"]))
        .mappings()
        .one()
    )
    price.update(
        id="price_claude_acceptance",
        model_id=second,
        input_price=Decimal(3),
        output_price=Decimal(15),
    )
    c.execute(insert(g.prices).values(**price))
    binding.update(model_id=second, price_version_id=price["id"])
    c.execute(insert(g.model_bindings).values(**binding))
    c.execute(
        update(nt.tenant_settings)
        .where(nt.tenant_settings.c.tenant_id == tenant)
        .values(default_model_id=model, ordered_model_ids=[model, second], version=3)
    )
    for uid, email, role in [
        ("resource_member", "member@example.test", "member"),
        ("resource_finance", "finance@example.test", "finance_viewer"),
    ]:
        c.execute(
            insert(t.users).values(
                id=uid,
                tenant_id=tenant,
                email=email,
                name=role,
                role="member",
                active=True,
                password_hash=hasher.hash(settings.admin_password),
                created_at=now(),
            )
        )
        c.execute(
            insert(nt.memberships).values(
                id=uid + "-membership",
                tenant_id=tenant,
                user_id=uid,
                role=role,
                status="active",
                joined_at=now(),
            )
        )
    c.execute(
        insert(wallets).values(
            tenant_id=tenant,
            balance=Decimal(100),
            reserved=Decimal(2),
            currency="USD",
            blocked=False,
            block_reason="",
        )
    )
    for index, (amount, balance, description, stamp) in enumerate(
        [
            ("100", "100.0054", "平台入账", "2026-10-01T01:00:00Z"),
            ("-0.0006", "100.0048", "计算助手调用费用", "2026-10-02T06:05:32Z"),
            ("-0.0048", "100.0000", "内容助手调用费用", "2026-10-02T06:12:05Z"),
        ]
    ):
        c.execute(
            insert(ledger_transactions).values(
                id=f"ledger_acceptance_{index}",
                tenant_id=tenant,
                type="credit" if index == 0 else "charge",
                amount=Decimal(amount),
                balance=Decimal(balance),
                description=description,
                fingerprint=f"ledger_{index}",
                created_at=stamp,
            )
        )
    agent = c.scalar(select(t.agents.c.id).where(t.agents.c.tenant_id == tenant))
    c.execute(
        insert(t.sessions).values(
            id="resource_session",
            tenant_id=tenant,
            user_id=owner["id"],
            agent_id=agent,
            title="资源验收",
            created_at=now(),
        )
    )
    for index, (status, stamp) in enumerate(
        [
            ("unresolved", "2026-10-02T06:05:32Z"),
            ("confirmed", "2026-10-02T06:12:05Z"),
            ("running", "2026-10-02T06:20:10Z"),
        ],
        1,
    ):
        run_id = f"run_demo_{index:03d}"
        c.execute(
            insert(t.runs).values(
                id=run_id,
                tenant_id=tenant,
                session_id="resource_session",
                user_id=owner["id"],
                agent_name="内容助手" if index == 2 else "计算助手",
                spec={},
                message="验收夹具",
                status="running" if index == 3 else "succeeded",
                idempotency_key=run_id,
                request_hash=hashlib.sha256(run_id.encode()).hexdigest(),
                created_at=stamp,
            )
        )
        c.execute(
            insert(t.calls).values(
                id=f"call_demo_{index:03d}",
                tenant_id=tenant,
                user_id=owner["id"],
                run_id=run_id,
                model_id=second if index == 2 else model,
                model="claude-3-5-sonnet" if index == 2 else "gpt-4o",
                agent_name="内容助手" if index == 2 else "计算助手",
                user_email=owner["email"],
                status=status,
                input_tokens=800 if index == 2 else 0,
                output_tokens=160 if index == 2 else 0,
                cost="0.0048" if index == 2 else "0",
                price_version=1,
                created_at=stamp,
            )
        )
        if index in (1, 3):
            c.execute(
                insert(reservations_table).values(
                    id=f"reservation_demo_{index}",
                    tenant_id=tenant,
                    user_id=owner["id"],
                    model_id=model,
                    run_id=run_id,
                    call_id=f"call_demo_{index:03d}",
                    fingerprint=f"reservation_demo_{index}",
                    amount=Decimal(1),
                    input_price=Decimal(5),
                    output_price=Decimal(15),
                    estimated_input_tokens=100,
                    max_output_tokens=4096,
                    rate_tokens=0,
                    rate_scopes="[]",
                    status="unresolved" if index == 1 else "reserved",
                    cost=Decimal(0),
                    overage=Decimal(0),
                    reason="等待用量确认" if index == 1 else "",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    for index in range(args.extra_calls):
        c.execute(
            insert(t.calls).values(
                id=f"call_pagination_{index:03d}",
                tenant_id=tenant,
                user_id=owner["id"],
                run_id="run_demo_001",
                model_id=model,
                model="gpt-4o",
                agent_name="分页验收",
                user_email=owner["email"],
                status="confirmed",
                input_tokens=10,
                output_tokens=5,
                cost="0.0001",
                price_version=1,
                created_at="2026-09-30T00:00:00Z",
            )
        )
    membership = dict(
        c.execute(select(nt.memberships).where(nt.memberships.c.user_id == owner["id"]))
        .mappings()
        .one()
    )
    expires = (
        datetime(2026, 10, 3, 6, 10, tzinfo=UTC).timestamp()
        if args.reference_expiry
        else datetime.now(UTC).timestamp() + 86400
    )
    for index, (status, kind, scope, stamp) in enumerate(
        [
            ("expired", "runs", "self", "2026-10-01T01:00:00Z"),
            ("ready", "calls", "self", "2026-10-02T06:10:00Z"),
            ("running", "calls", "tenant", "2026-10-02T06:22:00Z"),
        ],
        1,
    ):
        job = f"export_demo_{index:03d}"
        c.execute(
            insert(ot.exports).values(
                id=job,
                tenant_id=tenant,
                requested_by=owner["id"],
                membership_id=membership["id"],
                membership_version=membership["authz_version"],
                identity_version=owner["auth_version"],
                kind=kind,
                scope=scope,
                idempotency_key=job,
                request_hash=hashlib.sha256(job.encode()).hexdigest(),
                status=status,
                cutoff=stamp,
                row_count=1 if status == "ready" else 0,
                chunk_count=1 if status == "ready" else 0,
                fence=0,
                expires_at=expires if status == "ready" else 0,
                error_code="",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        if status == "ready":
            data = (
                b"Call ID,Model,Status\r\ncall_demo_002,claude-3-5-sonnet,confirmed\r\n"
            )
            directory = run / "exports" / tenant
            directory.mkdir(parents=True)
            filename = "acceptance-ready.csv"
            (directory / filename).write_bytes(data)
            c.execute(
                insert(ot.artifacts).values(
                    id="artifact_acceptance",
                    tenant_id=tenant,
                    job_id=job,
                    sequence=0,
                    filename=filename,
                    sha256=hashlib.sha256(data).hexdigest(),
                    byte_count=len(data),
                    row_count=1,
                    created_at=stamp,
                )
            )
(run / "fixtures.json").write_text(
    __import__("json").dumps(
        {"tenant": tenant, "model": model, "secondModel": second, "owner": owner["id"]},
        indent=2,
    )
)
uvicorn.run(app, host="127.0.0.1", port=args.port)
