"""Run the unmodified API over a fresh, migrated workspace acceptance database.

From the repository root:
  PYTHONPATH=. .venv/bin/python agent_platform/apps/web/tests/workspace_acceptance_server.py \
    --directory agent_platform/apps/web/.design-to-ui/runs/<run>/acceptance-server

This persists fixtures in SQLite; it does not replace HTTP handlers or services.
Existing directories are rejected, so it cannot reuse a production database.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, update
import uvicorn

from agent_platform.apps.api.main import create_app
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure import tables as t, tenancy_tables as nt
from agent_platform.modules.platform import hasher, now

PASSWORD = "Agent-ui-acceptance-2026"
DAILY_COUNTS = [110, 180, 148, 235, 265, 210, 132]


def start():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8036)
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=False)
    database_url = f"sqlite:///{directory / 'acceptance.db'}"
    os.environ["PLATFORM_DATABASE_URL"] = database_url
    root = Path(__file__).resolve().parents[4]
    command.upgrade(Config(str(root / "agent_platform/alembic.ini")), "head")
    settings = Settings(
        database_url=database_url,
        admin_email="admin@example.test",
        admin_password=PASSWORD,
        default_model="gpt-4o",
        default_model_input_price="1",
        default_model_output_price="3",
        web_directory=directory / "unused-web",
        tombstone_path=directory / "tombstones.jsonl",
        export_directory=directory / "exports",
    )
    app = create_app(settings)
    platform, database = app.state.platform, app.state.database
    platform.bootstrap()
    with database.read() as connection:
        owner = dict(connection.execute(select(t.users).where(t.users.c.email == settings.admin_email)).mappings().one())
        gpt = dict(connection.execute(select(t.models).where(t.models.c.alias == "gpt-4o")).mappings().one())
    tenant = owner["tenant_id"]
    claude = {**gpt, "id": "acceptance_claude", "name": "claude-3-5-sonnet", "alias": "claude-3-5-sonnet"}
    agents = [
        ("agent_calculator", "计算助手", "数学运算与时间查询", gpt["id"], "你是严谨的计算助手。先理解问题，再使用可用工具完成计算或查询当前时间，最后清晰解释结果。", 2),
        ("agent_content_editor", "内容助手", "根据提示词整理内容", None, "根据提示词整理内容。", 1),
        ("agent_draft_helper", "新建助手", "", None, "你是一位严谨、友好的智能助手。", 0),
    ]
    specs = {}
    stamp = now()

    def user(connection, identifier, email, role, scope=tenant):
        connection.execute(insert(t.users).values(id=identifier, tenant_id=scope, email=email, name=role, role="member", active=True, password_hash=hasher.hash(PASSWORD), created_at=stamp))
        connection.execute(insert(nt.memberships).values(id=identifier + "-membership", tenant_id=scope, user_id=identifier, role=role, status="active", joined_at=stamp))

    def run(connection, identifier, actor, agent, model, status, created, scope=tenant):
        session_id = "session_" + identifier
        frozen = {**agent, "model_id": model["id"], "model": model}
        connection.execute(insert(t.sessions).values(id=session_id, tenant_id=scope, user_id=actor, agent_id=agent["id"], title="验收会话 " + identifier, created_at=created))
        connection.execute(insert(t.runs).values(id=identifier, tenant_id=scope, user_id=actor, session_id=session_id, agent_name=agent["name"], spec=frozen, message="验收输入 " + identifier, status=status, error="", idempotency_key=identifier, request_hash=identifier, cancel_requested=status == "cancelled", fence=0, created_at=created, finished_at=created if status in {"succeeded", "cancelled"} else None))
        connection.execute(insert(t.messages).values(id="message_" + identifier, tenant_id=scope, session_id=session_id, run_id=identifier, role="user", content="验收输入 " + identifier, created_at=created))
        connection.execute(insert(t.run_events).values(run_id=identifier, tenant_id=scope, sequence=1, type="run." + status, data={"status": status}, created_at=created))

    with database.transaction("workspace-acceptance-fixtures") as connection:
        connection.execute(delete(t.agent_versions))
        connection.execute(delete(t.agents))
        connection.execute(update(t.tenants).where(t.tenants.c.id == tenant).values(name="工作空间"))
        connection.execute(update(t.models).where(t.models.c.id == gpt["id"]).values(max_output_tokens=8192))
        gpt["max_output_tokens"] = claude["max_output_tokens"] = 8192
        connection.execute(insert(t.models).values(**claude))
        for identifier, name, description, mid, prompt, version in agents:
            spec = dict(id=identifier, tenant_id=tenant, name=name, description=description, model_id=mid, system_prompt=prompt, temperature=0.7, max_steps=8, max_tokens=4096, tools=["calculator", "current_time"], published_version=version, created_at={"agent_calculator": "2026-10-03T03:00:00Z", "agent_content_editor": "2026-10-02T03:00:00Z", "agent_draft_helper": "2026-10-01T03:00:00Z"}[identifier])
            specs[identifier] = spec
            connection.execute(insert(t.agents).values(**spec))
            for v in range(1, version + 1):
                connection.execute(insert(t.agent_versions).values(agent_id=identifier, tenant_id=tenant, version=v, spec=spec, created_at=stamp))
        user(connection, "acceptance_member", "member@example.test", "member")
        user(connection, "acceptance_finance", "finance@example.test", "finance_viewer")
        user(connection, "acceptance_empty", "empty@example.test", "member")
        references = [
            ("run_demo_003", "agent_calculator", gpt, "running", "2026-10-02T06:20:00Z", "0.0021", 100),
            ("run_demo_002", "agent_content_editor", claude, "succeeded", "2026-10-02T06:12:00Z", "0.0048", 300),
            ("run_demo_001", "agent_calculator", gpt, "cancelled", "2026-10-02T06:05:00Z", "0.0006", 50),
        ]
        for identifier, aid, model, status, created, cost, tokens in references:
            run(connection, identifier, owner["id"], specs[aid], model, status, created)
            connection.execute(insert(t.calls).values(id="call_" + identifier, tenant_id=tenant, user_id=owner["id"], run_id=identifier, model_id=model["id"], model=model["alias"], agent_name=specs[aid]["name"], user_email=owner["email"], status="running" if status == "running" else "confirmed", input_tokens=tokens, output_tokens=0, cost=cost, price_version=1, created_at=created, error=""))
        run(connection, "run_member_active", "acceptance_member", specs["agent_calculator"], gpt, "running", "2026-10-02T06:18:00Z")
        run(connection, "run_member_history", "acceptance_member", specs["agent_content_editor"], claude, "succeeded", "2026-09-26T04:00:00Z")
        historical = []
        index = 0
        for day, count in enumerate(DAILY_COUNTS):
            for offset in range(count - (3 if day == 6 else 0)):
                model = gpt if index < 894 else claude
                created = (datetime(2026, 9, 26, 4, tzinfo=timezone.utc) + timedelta(days=day, seconds=offset)).isoformat().replace("+00:00", "Z")
                historical.append(dict(id=f"call_history_{index:04d}", tenant_id=tenant, user_id="acceptance_member", run_id="run_member_history", model_id=model["id"], model=model["alias"], agent_name="内容助手", user_email="member@example.test", status="confirmed" if index < 1232 else "failed", input_tokens=1000 if index < 1276 else 8050, output_tokens=0, cost="0.0097" if index < 1276 else "0.0953", price_version=1, created_at=created, error=""))
                index += 1
        connection.execute(insert(t.calls), historical)

        # Cursor/cancellation checks have their own tenant and identity; neither
        # their run count nor test mutations change the visual baseline tenant.
        scope = "acceptance_pagination_tenant"
        connection.execute(insert(t.tenants).values(id=scope, name="分页验收空间", created_at=stamp))
        connection.execute(insert(nt.tenant_settings).values(tenant_id=scope, status="active", created_at=stamp, updated_at=stamp))
        connection.execute(insert(nt.entitlements).values(tenant_id=scope, updated_at=stamp))
        page_model = {**gpt, "id": "pagination_model", "tenant_id": scope}
        connection.execute(insert(t.models).values(**page_model))
        page_agent = {**specs["agent_calculator"], "id": "pagination_agent", "tenant_id": scope, "model_id": page_model["id"], "published_version": 1}
        connection.execute(insert(t.agents).values(**page_agent))
        connection.execute(insert(t.agent_versions).values(agent_id=page_agent["id"], tenant_id=scope, version=1, spec=page_agent, created_at=stamp))
        user(connection, "acceptance_pagination", "pagination@example.test", "member", scope)
        for index in range(60):
            status = "running" if index == 59 else "queued" if index == 58 else "succeeded"
            created = (datetime(2026, 9, 25, 4, tzinfo=timezone.utc) + timedelta(minutes=index)).isoformat().replace("+00:00", "Z")
            run(connection, f"run_page_{index:03d}", "acceptance_pagination", page_agent, page_model, status, created, scope)

    context = platform.identity.context(owner["id"], tenant)
    totals = platform.dashboard(context)
    assert (totals["requests"], totals["tokens"], Decimal(totals["cost"]), totals["success_rate"], totals["active_runs"]) == (1280, 1284500, Decimal("12.4800"), 96.4, 2)
    assert [row["requests"] for row in totals["daily"]] == DAILY_COUNTS
    assert {row["model"]: row["requests"] for row in totals["models"]} == {"gpt-4o": 896, "claude-3-5-sonnet": 384}
    (directory / "fixtures-ready.json").write_text(json.dumps({"tenant_id": tenant, "owner_id": owner["id"], "dashboard": totals, "pagination_tenant_id": scope}, ensure_ascii=False, indent=2))
    print(f"Workspace acceptance database: {directory / 'acceptance.db'}", flush=True)
    print(f"Verified real dashboard: {json.dumps(totals, ensure_ascii=False)}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    start()
