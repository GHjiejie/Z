"""Offline acceptance for real checkpointed runs, API writes and human forms."""

import asyncio
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field

from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.hitl_checkpoint import DatabaseCheckpointer
from agent_permissions.hitl_runtime import safe_model_message
from agent_permissions.models import UserCreate
from agent_permissions.test_support import grant_tools
from agent_permissions.tracing import ChatTracing, TracingSettings


def events(response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def responses_sse(response: dict) -> httpx.Response:
    frames = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        }
    ]
    for index, item in enumerate(response["output"]):
        position = {"output_index": index, "item_id": item["id"]}
        if item["type"] == "function_call":
            frames.append(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**item, "arguments": "", "status": "in_progress"},
                }
            )
            encoded = item["arguments"]
            middle = len(encoded) // 2
            for delta in (encoded[:middle], encoded[middle:]):
                frames.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        **position,
                        "delta": delta,
                    }
                )
        else:
            frames.append(
                {
                    "type": "response.output_text.delta",
                    **position,
                    "content_index": 0,
                    "delta": item["content"][0]["text"],
                }
            )
            frames.append(
                {
                    "type": "response.output_text.done",
                    **position,
                    "content_index": 0,
                    "text": item["content"][0]["text"],
                }
            )
        frames.append(
            {"type": "response.output_item.done", "output_index": index, "item": item}
        )
    frames.append({"type": "response.completed", "response": response})
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content="".join(
            "data: " + json.dumps({**frame, "sequence_number": i}) + "\n\n"
            for i, frame in enumerate(frames)
        ),
    )


class InteractionModel(BaseChatModel):
    model_name: str = "hitl-fixture"
    operation: str = "role.create"
    arguments: dict = Field(
        default_factory=lambda: {"body": {"code": "new_role", "display_name": "新角色"}}
    )
    prepare: bool = False
    second: bool = False
    recorded: list[str] = Field(default_factory=list)
    fail_after_tool: bool = False

    @property
    def _llm_type(self) -> str:
        return "offline-hitl"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.recorded.append(repr(messages))
        if isinstance(messages[-1], ToolMessage):
            if self.fail_after_tool:
                raise RuntimeError("offline-failure")
            result = json.loads(messages[-1].content)
            reply = AIMessage(
                content="操作已完成"
                if result.get("ok")
                else json.dumps(result, ensure_ascii=False)
            )
        else:
            name = (
                "request_tool_input"
                if self.prepare
                else "agent_users_" + self.operation.replace(".", "_")
            )
            arguments = (
                {"operation_id": self.operation, "arguments": self.arguments}
                if self.prepare
                else self.arguments
            )
            calls = [{"name": name, "args": arguments, "id": "call_one"}]
            if self.second:
                calls.append(
                    {
                        "name": "agent_users_role_create",
                        "args": {
                            "body": {
                                "code": "second_role",
                                "display_name": "第二个角色",
                            }
                        },
                        "id": "call_two",
                    }
                )
            reply = AIMessage(content="", tool_calls=calls)
        reply.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        }
        return ChatResult(generations=[ChatGeneration(message=reply)])


class ModelMessageTests(unittest.TestCase):
    def test_schema_secrets_and_malformed_containers_in_normalized_blocks(self):
        tool = {
            "binding": {"operationId": "example"},
            "definition": {
                "name": "example",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "object",
                            "properties": {
                                "pin": {"type": "string", "writeOnly": True},
                                "phrase": {"type": "string", "format": "password"},
                                "label": {"type": "string"},
                            },
                        }
                    },
                },
            },
        }
        for body, expected in [
            (
                {"pin": "private-pin", "phrase": "private-phrase", "label": "公开"},
                {"body": {"label": "公开"}},
            ),
            ("opaque-private", {}),
        ]:
            with self.subTest(body_type=type(body).__name__):
                call = {
                    "type": "tool_call",
                    "id": "call_one",
                    "name": "example",
                    "args": {"body": body},
                }
                text = {"type": "text", "text": "正在核对"}
                message = AIMessage(
                    content=[text, {**call, "index": 1}], tool_calls=[call]
                )
                safe = safe_model_message(message, [tool])
                self.assertEqual(safe.tool_calls[0]["args"], expected)
                self.assertEqual(
                    safe.content, [text, {**call, "index": 1, "args": expected}]
                )
                self.assertEqual(message.tool_calls[0]["args"]["body"], body)


class HitlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.settings_for(self.folder / "hitl.sqlite3")
        self.now = datetime(2026, 11, 17, tzinfo=UTC)
        self.model = InteractionModel()
        self.app = self.application()
        self.client = self.enterContext(TestClient(self.app))
        self.app.state.service.bootstrap_admin(
            UserCreate(
                username="admin", display_name="管理员", password="test-password"
            )
        )
        data = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-password"},
        ).json()["data"]
        self.user_id = data["user"]["id"]
        self.headers = {"Authorization": "Bearer " + data["session_token"]}
        grant_tools(self.client, self.headers, self.user_id)

    def settings_for(self, path):
        return Settings(database_path=path)

    def application(self):
        return create_app(
            self.settings,
            clock=lambda: self.now,
            chat_model_factory=lambda: self.model,
            chat_tracing=ChatTracing(TracingSettings()),
        )

    def start(
        self, *, conversation: str | None = None, request_id: str | None = None
    ) -> dict:
        response = self.client.post(
            "/api/v1/chat/runs",
            headers=self.headers,
            json={
                "model": self.model.model_name,
                "messages": [{"role": "user", "content": "请处理操作"}],
                "client_request_id": request_id or uuid4().hex,
                "conversation_id": conversation or uuid4().hex,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return [item["run"] for item in events(response) if "run" in item][-1]

    def submit(
        self,
        run: dict,
        *,
        values: dict | None = None,
        secrets: dict | None = None,
        submission: str = "submission-one",
        version: int | None = None,
        decision: str = "submit",
        client=None,
    ):
        item = run["interaction"]
        return (client or self.client).post(
            f"/api/v1/chat/runs/{run['id']}/interactions/{item['id']}/resume",
            headers=self.headers,
            json={
                "submission_id": submission,
                "expected_version": version or item["version"],
                "decision": decision,
                "values": values or {},
                "sensitive_values": secrets or {},
            },
        )

    def final(self, response) -> dict:
        self.assertEqual(response.status_code, 200, response.text)
        return [item["run"] for item in events(response) if "run" in item][-1]

    def snapshot(self, run: dict) -> dict:
        response = self.client.get(
            f"/api/v1/chat/runs/{run['id']}", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_write_pauses_edits_executes_once_and_retries_return_receipt(self):
        run = self.start()
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).status_code,
            404,
        )
        values = {"/body/display_name": "人工修改的名称"}
        done = self.final(self.submit(run, values=values))
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["usage"]["total_tokens"], 24)
        self.assertEqual(done["executions"][0]["status"], "succeeded")
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).json()[
                "data"
            ]["display_name"],
            values["/body/display_name"],
        )
        self.assertEqual(
            self.final(self.submit(run, values=values))["status"], "completed"
        )
        self.assertEqual(
            self.submit(run, values={"/body/display_name": "不同参数"}).status_code, 409
        )
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE action='role.create' AND outcome='success'"
                ).fetchone()[0],
                1,
            )

    def test_missing_parameters_password_stays_out_of_graph_and_history(self):
        self.model.operation, self.model.arguments, self.model.prepare = (
            "user.create",
            {},
            True,
        )
        run = self.start()
        self.assertEqual(run["interaction"]["kind"], "collect_parameters")
        secret = "human-secret-Only-77654"
        done = self.final(
            self.submit(
                run,
                values={"/body/username": "new_user", "/body/display_name": "新用户"},
                secrets={"/body/password": secret},
            )
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["executions"][0]["result"]["httpStatus"], 201)
        self.assertNotIn(secret, repr(done))
        self.assertNotIn(secret, repr(self.model.recorded))
        saver = DatabaseCheckpointer(self.app.state.database)
        for checkpoint in saver.list({"configurable": {"thread_id": run["id"]}}):
            self.assertNotIn(secret, repr(checkpoint))
        with self.app.state.database.transaction() as connection:
            for table in [
                "agent_chat_runs",
                "agent_interactions",
                "agent_tool_executions",
                "audit_events",
            ]:
                self.assertNotIn(
                    secret,
                    repr(
                        [
                            dict(row)
                            for row in connection.execute(f"SELECT * FROM {table}")
                        ]
                    ),
                )
        login = self.client.post(
            "/api/v1/auth/login", json={"username": "new_user", "password": secret}
        )
        self.assertEqual(login.status_code, 200)

    def test_responses_tool_arguments_are_safe_in_checkpoints_and_next_request(self):
        self.check_responses_replay(streaming=False)

    def test_streamed_responses_tool_arguments_are_safe_after_assembly(self):
        self.check_responses_replay(streaming=True)

    def check_responses_replay(self, *, streaming):
        proposed = "model-proposed-password"
        malformed = "invalid-json-private-value"
        form_secret = "human-entered-password"
        body = {"username": "responses_user", "display_name": "表单用户"}

        def function_call(key, name, arguments):
            return {
                "type": "function_call",
                "id": "fc_" + key,
                "call_id": "call_" + key,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }

        calls = [
            function_call(
                "read",
                "agent_users_user_self_read",
                json.dumps({"authorization": proposed}),
            ),
            function_call("bad", "agent_users_user_create", "unparsed: " + malformed),
            function_call(
                "form",
                "request_tool_input",
                json.dumps(
                    {
                        "operation_id": "user.create",
                        "arguments": {"body": {**body, "password": proposed}},
                    }
                ),
            ),
        ]
        requests = []

        def respond(request):
            self.assertEqual(request.url.path, "/v1/responses")
            requests.append(json.loads(request.content))
            message = {
                "type": "message",
                "id": f"msg_{len(requests)}",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "核对资料" if len(requests) == 1 else "已完成",
                        "annotations": [],
                    }
                ],
            }
            response = {
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "model": "hitl-fixture",
                "status": "completed",
                "output": [message, *calls] if len(requests) == 1 else [message],
            }
            self.assertEqual(requests[-1].get("stream", False), streaming)
            return (
                responses_sse(response)
                if streaming
                else httpx.Response(200, json=response)
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addCleanup(self.client.portal.call, http.aclose)
        self.model = ChatOpenAI(
            model="hitl-fixture",
            api_key="offline",
            base_url="https://model.test/v1",
            use_responses_api=True,
            streaming=streaming,
            disable_streaming=not streaming,
            http_socket_options=(),
            http_async_client=http,
            max_retries=0,
        )
        run = self.start()
        self.assertEqual(run["status"], "waiting", run)
        self.assertEqual(run["interaction"]["kind"], "collect_parameters")
        result = self.submit(run, secrets={"/body/password": form_secret})
        done = self.final(result)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(
            [e["status"] for e in done["executions"]], ["succeeded", "succeeded"]
        )
        self.assertEqual(len(requests), 2)
        replay = requests[-1]["input"]
        forwarded = [item for item in replay if item["type"] == "function_call"]
        self.assertEqual(
            [(c["id"], c["call_id"]) for c in forwarded],
            [("fc_read", "call_read"), ("fc_form", "call_form")],
        )
        self.assertEqual(
            [json.loads(c["arguments"]) for c in forwarded],
            [{}, {"operation_id": "user.create", "arguments": {"body": body}}],
        )
        self.assertEqual(
            [
                item["call_id"]
                for item in replay
                if item["type"] == "function_call_output"
            ],
            ["call_read", "call_form"],
        )
        self.assertIn("核对资料", repr(replay))
        checkpoints = list(
            DatabaseCheckpointer(self.app.state.database).list(
                {"configurable": {"thread_id": run["id"]}}
            )
        )
        self.assertTrue(checkpoints)
        for secret in (proposed, malformed, form_secret):
            for evidence in (run, result.text, requests[-1], checkpoints):
                self.assertNotIn(secret, repr(evidence))

    def test_restart_restores_same_checkpoint_and_call(self):
        run = self.start()
        with TestClient(self.application()) as restarted:
            stored = restarted.get(
                f"/api/v1/chat/runs/{run['id']}", headers=self.headers
            ).json()["data"]
            self.assertEqual(stored["interaction"]["id"], run["interaction"]["id"])
            done = self.final(self.submit(stored, client=restarted))
            self.assertEqual(done["status"], "completed")
            self.assertEqual(len(self.model.recorded), 2)

    def test_resuming_a_legacy_checkpoint_cleans_future_messages_and_checkpoints(self):
        run = self.start()
        saver = DatabaseCheckpointer(self.app.state.database)
        config = {"configurable": {"thread_id": run["id"]}}
        saved = saver.get_tuple(config)
        message = next(
            m
            for m in saved.checkpoint["channel_values"]["messages"]
            if isinstance(m, AIMessage)
        )
        call = message.tool_calls[0]
        legacy_secret = "legacy-model-private"
        message.content = [
            {
                "type": "function_call",
                "name": call["name"],
                "call_id": call["id"],
                "id": "fc_legacy",
                "arguments": json.dumps({**call["args"], "password": legacy_secret}),
            }
        ]
        saver.put(saved.parent_config or config, saved.checkpoint, saved.metadata, {})
        self.assertIn(legacy_secret, repr(saver.get_tuple(config)))
        self.assertEqual(self.final(self.submit(run))["status"], "completed")
        self.assertNotIn(legacy_secret, repr(self.model.recorded))
        newer = [
            item
            for item in saver.list(config)
            if item.checkpoint["id"] > saved.checkpoint["id"]
        ]
        self.assertTrue(newer)
        self.assertNotIn(legacy_secret, repr(newer))

    def test_read_only_complete_input_has_no_interaction(self):
        self.model.operation, self.model.arguments = "user.self.read", {}
        run = self.start()
        self.assertEqual(run["status"], "completed")
        self.assertIsNone(run["interaction"])
        self.assertEqual(
            run["executions"][0]["result"]["body"]["data"]["username"], "admin"
        )

    def test_invalid_fields_and_hidden_arguments_do_not_consume_confirmation(self):
        run = self.start()
        for values, secrets in [
            ({"/body/code": "BAD"}, {}),
            ({"/path/user_id": "someone"}, {}),
            ({"/header/Authorization": "forged"}, {}),
            ({}, {"/body/display_name": "secret-channel"}),
        ]:
            self.assertEqual(
                self.submit(run, values=values, secrets=secrets).status_code, 422
            )
            self.assertEqual(self.snapshot(run)["status"], "waiting")
        self.assertEqual(self.submit(run, version=9).status_code, 412)
        self.assertEqual(self.final(self.submit(run))["status"], "completed")

    def test_cancel_and_expiry_prevent_execution(self):
        run = self.start()
        self.client.post(f"/api/v1/chat/runs/{run['id']}/cancel", headers=self.headers)
        self.assertEqual(self.snapshot(run)["status"], "cancelled")
        self.assertEqual(self.submit(run).status_code, 409)
        expired = self.start()
        self.now += timedelta(seconds=1801)
        self.assertEqual(self.submit(expired).status_code, 410)
        self.assertEqual(self.submit(expired, decision="cancel").status_code, 410)
        self.assertEqual(self.snapshot(expired)["status"], "expired")
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).status_code,
            404,
        )

    def test_card_cancel_checks_version_and_replays_only_the_same_decision(self):
        run = self.start()
        version = run["interaction"]["version"]
        wrong = self.submit(run, decision="cancel", version=version + 1)
        self.assertEqual(wrong.status_code, 412)
        self.assertEqual(wrong.json()["error"]["code"], "INTERACTION_VERSION_CONFLICT")
        self.assertEqual(
            self.submit(
                run, decision="cancel", values={"/body/code": "unused"}
            ).status_code,
            422,
        )
        self.assertEqual(self.snapshot(run)["interaction"], run["interaction"])
        grant_tools(
            self.client, self.headers, self.user_id, ["role.create"], enabled=False
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: self.final(self.submit(run, decision="cancel")), range(2)
                )
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["status"], "cancelled")
        self.assertIsNone(results[0]["interaction"])
        self.assertEqual(results[0]["executions"], [])
        for changed in (
            self.submit(run),
            self.submit(run, decision="cancel", version=version + 1),
        ):
            self.assertEqual(changed.status_code, 409)
            self.assertEqual(changed.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        store = self.app.state.run_store
        card = store.interaction(run["id"], run["interaction"]["id"], self.user_id)
        self.assertEqual((card["status"], card["version"]), ("cancelled", version + 1))
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE request_id=? AND action='agent.run.cancelled'",
                    (run["id"],),
                ).fetchone()[0],
                1,
            )

    def test_cross_user_cannot_read_or_resume(self):
        run = self.start()
        self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "other_user",
                "display_name": "其他用户",
                "password": "test-password",
            },
        )
        token = self.client.post(
            "/api/v1/auth/login",
            json={"username": "other_user", "password": "test-password"},
        ).json()["data"]["session_token"]
        other = {"Authorization": "Bearer " + token}
        self.assertEqual(
            self.client.get(
                f"/api/v1/chat/runs/{run['id']}", headers=other
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/chat/runs/{run['id']}/cancel", headers=other
            ).status_code,
            404,
        )

    def test_revocation_and_tool_contract_change_block_old_form(self):
        run = self.start()
        grant_tools(
            self.client, self.headers, self.user_id, ["role.create"], enabled=False
        )
        self.assertEqual(self.submit(run).status_code, 403)
        grant_tools(self.client, self.headers, self.user_id, ["role.create"])
        registry = self.app.state.tool_registry
        candidate = copy.deepcopy(registry.snapshot())
        from api_tools.compiler import digest

        candidate["sourceRevision"] = "f" * 64
        candidate["bundleDigest"] = digest(
            {key: value for key, value in candidate.items() if key != "bundleDigest"}
        )
        registry.activate(candidate, expected=candidate)
        self.assertEqual(self.submit(run).status_code, 409)

    def test_same_conversation_and_create_retry_do_not_start_extra_model_calls(self):
        run = self.start(conversation="one", request_id="one")
        replay = self.start(conversation="one", request_id="one")
        self.assertEqual(run["id"], replay["id"])
        self.assertEqual(len(self.model.recorded), 1)
        response = self.client.post(
            "/api/v1/chat/runs",
            headers=self.headers,
            json={
                "model": self.model.model_name,
                "messages": [{"role": "user", "content": "第二条"}],
                "conversation_id": "one",
                "client_request_id": "two",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_two_submissions_only_write_once(self):
        run = self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.submit(run), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM roles WHERE code='new_role'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM agent_tool_executions WHERE run_id=?",
                    (run["id"],),
                ).fetchone()[0],
                1,
            )

    def test_approval_and_cancellation_cannot_both_win(self):
        run = self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            approve = pool.submit(self.submit, run, submission="approve")
            cancel = pool.submit(
                self.submit, run, submission="cancel", decision="cancel"
            )
            approved, cancelled = approve.result(), cancel.result()
        self.assertEqual(
            sorted([approved.status_code, cancelled.status_code]), [200, 409]
        )
        approval_won = approved.status_code == 200
        snapshot = self.snapshot(run)
        self.assertEqual(
            snapshot["status"], "completed" if approval_won else "cancelled"
        )
        self.assertEqual(len(snapshot["executions"]), int(approval_won))
        role = self.client.get("/api/v1/roles/new_role", headers=self.headers)
        self.assertEqual(role.status_code, 200 if approval_won else 404)

    def test_multi_tool_calls_are_confirmed_separately(self):
        self.model.second = True
        first = self.start()
        second = self.final(self.submit(first))
        self.assertEqual(second["status"], "waiting")
        self.assertNotEqual(first["interaction"]["id"], second["interaction"]["id"])
        self.assertEqual(
            first["interaction"]["version"], second["interaction"]["version"]
        )
        stale = self.submit(first, decision="cancel", submission="cancel-old-card")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "INTERACTION_ALREADY_RESOLVED")
        self.assertEqual(self.snapshot(second)["interaction"], second["interaction"])
        self.assertEqual(
            self.client.get(
                "/api/v1/roles/second_role", headers=self.headers
            ).status_code,
            404,
        )
        final = self.final(self.submit(second))
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(final["executions"]), 2)

    def test_business_success_survives_reply_failure(self):
        self.model.fail_after_tool = True
        run = self.start()
        failed = self.final(self.submit(run))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["executions"][0]["status"], "succeeded")
        self.assertEqual(
            self.final(self.submit(run))["executions"][0]["status"], "succeeded"
        )

    def test_sqlite_schema_three_migration_rolls_back_and_preserves_grants(self):
        from unittest.mock import patch

        from agent_permissions.database import Database
        from agent_permissions.hitl_schema import HITL_SCHEMA

        if self.settings.database_url:
            self.skipTest(
                "SQLite-specific DDL rollback; PostgreSQL migration is covered separately"
            )
        database = self.app.state.database
        with database.transaction(write=True) as connection:
            for table in [
                "agent_checkpoint_writes",
                "agent_checkpoints",
                "agent_tool_executions",
                "agent_interactions",
                "agent_chat_runs",
            ]:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=3")
        original = Database._execute_schema

        def fail_after_ddl(connection, schema):
            original(connection, schema)
            if schema == HITL_SCHEMA:
                raise RuntimeError("test-schema-upgrade-failure")

        with (
            patch.object(Database, "_execute_schema", staticmethod(fail_after_ddl)),
            self.assertRaises(RuntimeError),
        ):
            database.initialize()
        with database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='agent_chat_runs'"
                ).fetchone()
            )
        database.initialize()
        with database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                connection.execute(
                    "SELECT enabled FROM user_agent_tool_grants WHERE user_id=? AND operation_id='role.create'",
                    (self.user_id,),
                ).fetchone()[0],
                1,
            )

    def test_direct_tool_write_requires_confirmation(self):
        revision = self.app.state.tool_registry.snapshot()["bundleDigest"]
        response = self.client.post(
            "/agent-tools/call",
            headers=self.headers,
            json={
                "name": "agent_users_role_create",
                "arguments": self.model.arguments,
                "revision": revision,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "INTERACTION_REQUIRED")

    def test_resource_version_conflict_and_explicit_refresh(self):
        user = self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "target_user",
                "display_name": "原名称",
                "password": "test-password",
            },
        ).json()["data"]
        self.model.operation, self.model.arguments = (
            "user.update",
            {"path": {"user_id": user["id"]}, "body": {"display_name": "确认的名称"}},
        )
        run = self.start()
        self.client.patch(
            f"/api/v1/users/{user['id']}",
            headers={**self.headers, "If-Match": f'"v{user["version"]}"'},
            json={"display_name": "其他人修改"},
        )
        self.assertEqual(self.submit(run).status_code, 412)
        item = run["interaction"]
        response = self.client.post(
            f"/api/v1/chat/runs/{run['id']}/interactions/{item['id']}/refresh",
            headers=self.headers,
            json={"expected_version": item["version"], "request_id": "refresh-one"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()["data"]
        self.assertNotEqual(updated["interaction"]["id"], item["id"])
        self.assertEqual(updated["interaction"]["target"]["display_name"], "其他人修改")
        self.assertEqual(self.final(self.submit(updated))["status"], "completed")

    def recover(self, run):
        return self.client.post(
            f"/api/v1/chat/runs/{run['id']}/recover", headers=self.headers
        )

    def test_interrupted_model_output_is_replaced_when_same_node_retries(self):
        from starlette.requests import Request

        from agent_permissions.hitl_runtime import run_events
        from agent_permissions.service import RequestInfo
        from agent_permissions.test_chat_api import BusinessModel

        class SlowModel(BusinessModel):
            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="未完成的前缀")
                )
                await asyncio.sleep(10)

        store = self.app.state.run_store
        run, _ = store.create(
            self.user_id,
            "interrupted-output",
            "interrupted-output",
            {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "完整回复"}],
                "tool_choice": "none",
            },
        )

        async def exercise():
            request = Request(
                {
                    "type": "http",
                    "app": self.app,
                    "headers": [
                        (b"authorization", self.headers["Authorization"].encode())
                    ],
                    "client": ("test", 0),
                    "state": {"info": RequestInfo("test-output", "test")},
                }
            )
            stream = run_events(request, run, SlowModel())
            async for event in stream:
                if event["type"] == "text.delta":
                    break
            await stream.aclose()

        asyncio.run(exercise())
        self.assertEqual(self.snapshot(run)["content"], "未完成的前缀")
        self.app.state.chat_model = BusinessModel(mode="echo")
        done = self.final(self.recover(run))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["content"], "完整回复")

    def test_recover_committed_write_after_model_failure_never_replays_it(self):
        self.model.fail_after_tool = True
        run = self.start()
        failed = self.final(self.submit(run))
        self.model.fail_after_tool = False
        done = self.final(self.recover(failed))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(done["executions"]), 1)
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE action='role.create' AND outcome='success'"
                ).fetchone()[0],
                1,
            )

    def test_recover_accepted_submission_before_execution_requires_fresh_confirmation(
        self,
    ):
        run = self.start()
        store = self.app.state.run_store
        raw = store.raw(run["id"], self.user_id)
        interaction = store.interaction(
            run["id"], run["interaction"]["id"], self.user_id
        )
        arguments = json.loads(interaction["document"])["arguments"]
        arguments["body"]["display_name"] = "中断前已编辑的角色名"
        active, _ = store.submit(
            raw, interaction, "lost-submission", 1, arguments, "test-fingerprint"
        )
        store.update(active, status="interrupted")
        recovered = self.final(self.recover(run))
        self.assertEqual(recovered["status"], "waiting")
        self.assertNotEqual(recovered["interaction"]["id"], run["interaction"]["id"])
        self.assertEqual(
            next(
                field["value"]
                for field in recovered["interaction"]["fields"]
                if field["pointer"] == "/body/display_name"
            ),
            "中断前已编辑的角色名",
        )
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).status_code,
            404,
        )
        done = self.final(self.submit(recovered))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(done["executions"]), 1)

    def test_lost_http_response_recovers_atomic_business_receipt(self):
        from unittest.mock import patch

        import httpx

        original = httpx.AsyncClient.post

        async def lost(client, url, **kwargs):
            response = await original(client, url, **kwargs)
            if url == "/agent-tools/call":
                raise httpx.ReadError("private-transport-input")
            return response

        run = self.start()
        with patch.object(httpx.AsyncClient, "post", lost):
            done = self.final(self.submit(run))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["executions"][0]["status"], "succeeded")
        self.assertTrue(done["executions"][0]["result"]["ok"])
        self.assertNotIn("private-transport-input", json.dumps(done))

    def test_receipt_survives_crash_before_tool_checkpoint_and_later_revocation(self):
        from unittest.mock import patch

        run = self.start()
        with patch.object(
            self.app.state.run_store,
            "finish_execution",
            side_effect=RuntimeError("after-commit"),
        ):
            failed = self.final(self.submit(run))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["executions"][0]["status"], "succeeded")
        grant_tools(
            self.client, self.headers, self.user_id, ["role.create"], enabled=False
        )
        done = self.final(self.recover(run))
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["content"], "操作已完成")
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_events WHERE action='role.create' AND outcome='success'"
                ).fetchone()[0],
                1,
            )

    def test_live_rbac_revocation_keeps_grant_but_rejects_resume(self):
        self.client.post(
            "/api/v1/roles",
            headers=self.headers,
            json={
                "code": "creator",
                "display_name": "创建者",
                "permissions": ["role.create"],
            },
        )
        user = self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "creator",
                "display_name": "创建者",
                "password": "test-password",
                "roles": ["creator"],
            },
        ).json()["data"]
        grant_tools(self.client, self.headers, user["id"], ["role.create"])
        admin = self.headers
        token = self.client.post(
            "/api/v1/auth/login",
            json={"username": "creator", "password": "test-password"},
        ).json()["data"]["session_token"]
        self.headers = {"Authorization": "Bearer " + token}
        run = self.start()
        self.client.patch(
            "/api/v1/roles/creator",
            headers={**admin, "If-Match": '"v1"'},
            json={"permissions": []},
        )
        self.assertEqual(self.submit(run).status_code, 403)
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT enabled FROM user_agent_tool_grants WHERE user_id=? AND operation_id='role.create'",
                    (user["id"],),
                ).fetchone()[0],
                1,
            )

    def test_owner_options_use_business_endpoints_and_target_selection_reconfirms(self):
        self.model.operation, self.model.arguments, self.model.prepare = (
            "user.update",
            {"body": {"display_name": "目标的新名称"}},
            True,
        )
        run = self.start()
        url = f"/api/v1/chat/runs/{run['id']}/interactions/{run['interaction']['id']}/options"
        options = self.client.get(
            url, headers=self.headers, params={"field": "/path/user_id", "q": "admin"}
        )
        self.assertEqual(options.status_code, 200, options.text)
        self.assertEqual(options.json()["data"][0]["value"], self.user_id)
        confirmed = self.final(self.submit(run, values={"/path/user_id": self.user_id}))
        self.assertEqual(confirmed["status"], "waiting")
        self.assertEqual(confirmed["interaction"]["target"]["username"], "admin")
        self.assertEqual(
            self.final(self.submit(run, values={"/path/user_id": self.user_id}))[
                "interaction"
            ]["id"],
            confirmed["interaction"]["id"],
        )
        self.assertEqual(self.final(self.submit(confirmed))["status"], "completed")

    def test_refresh_is_idempotent_and_run_version_is_checked(self):
        run = self.start()
        item = run["interaction"]
        url = f"/api/v1/chat/runs/{run['id']}/interactions/{item['id']}/refresh"
        data = {"request_id": "refresh-retry", "expected_version": 1}
        first = self.client.post(url, headers=self.headers, json=data).json()["data"]
        retry = self.client.post(url, headers=self.headers, json=data).json()["data"]
        self.assertEqual(first["interaction"]["id"], retry["interaction"]["id"])
        with self.app.state.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE agent_chat_runs SET graph_version=99 WHERE id=?", (run["id"],)
            )
        self.assertEqual(self.submit(first).status_code, 409)

    def test_retention_purges_private_context_without_reusing_idempotency_key(self):
        run = self.start(conversation="retained", request_id="retained-key")
        self.final(self.submit(run))
        self.now += timedelta(days=2)
        token = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-password"},
        ).json()["data"]["session_token"]
        self.headers = {"Authorization": "Bearer " + token}
        snapshot = self.snapshot(run)
        self.assertTrue(snapshot["purged"])
        self.assertEqual(snapshot["content"], "")
        retried = self.start(conversation="retained", request_id="retained-key")
        self.assertEqual(retried["id"], run["id"])
        self.assertTrue(retried["purged"])
        with self.app.state.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM agent_checkpoints WHERE thread_id=?",
                    (run["id"],),
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
