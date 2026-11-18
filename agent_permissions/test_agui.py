"""AG-UI wire compatibility and the real permission/HITL execution boundary."""

import json
import unittest
from uuid import uuid4

from ag_ui.core import Event
from langchain_core.messages import AIMessage
from pydantic import TypeAdapter

from agent_permissions import test_hitl
from agent_permissions.test_hitl import InteractionModel, events
from agent_permissions.test_support import grant_tools
from agent_permissions.workflow import reasoning_text


class SummaryModel(InteractionModel):
    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        message = result.generations[0].message
        text = message.content
        if message.tool_calls:
            text = (
                "先查找工具。"
                if message.tool_calls[0]["name"] == "search_tools"
                else "正在查询🔎。"
            )
        message.content = [
            {
                "type": "reasoning",
                "reasoning": "先核对当前账号，再使用已授权的查询工具。",
            },
            {
                "type": "text",
                "text": text,
            },
        ]
        return result


class AguiTests(unittest.TestCase):
    settings_for = test_hitl.HitlTests.settings_for
    application = test_hitl.HitlTests.application
    setUp = test_hitl.HitlTests.setUp

    def input(self, *, thread=None, **props):
        return {
            "protocolVersion": "1.0",
            "threadId": thread or uuid4().hex,
            "runId": uuid4().hex,
            "messages": [{"id": "human", "role": "user", "content": "请处理操作"}],
            "state": {},
            "tools": [],
            "context": [],
            "forwardedProps": {"model": self.model.model_name, **props},
        }

    def call(self, payload):
        response = self.client.post("/api/v1/ag-ui", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        stream = events(response)
        for event in stream:
            TypeAdapter(Event).validate_python(event)
        self.assertEqual(stream[0]["type"], "RUN_STARTED")
        self.assertEqual(stream[0]["threadId"], payload["threadId"])
        self.assertEqual(stream[0]["runId"], payload["runId"])
        return stream

    @staticmethod
    def snapshot(stream):
        return [
            event["snapshot"]["run"]
            for event in stream
            if event["type"] == "STATE_SNAPSHOT"
        ][-1]

    def continuation(self, run, **submission):
        body = self.input(
            thread=run["conversation_id"], action="resume", businessRunId=run["id"]
        )
        body["messages"] = []
        body["resume"] = [
            {
                "interruptId": run["interaction"]["id"],
                "status": "resolved",
                "payload": {
                    "expected_version": run["interaction"]["version"],
                    "values": {},
                    "sensitive_values": {},
                    **submission,
                },
            }
        ]
        return body

    def test_standard_events_and_actual_read_tool_are_visible(self):
        self.model.operation, self.model.arguments = "user.self.read", {}
        payload = self.input()
        stream = self.call(payload)
        kinds = [event["type"] for event in stream]
        self.assertLess(kinds.index("STEP_STARTED"), kinds.index("TOOL_CALL_START"))
        self.assertLess(kinds.index("TOOL_CALL_START"), kinds.index("TOOL_CALL_ARGS"))
        self.assertLess(kinds.index("TOOL_CALL_END"), kinds.index("TOOL_CALL_RESULT"))
        self.assertLess(kinds.index("TOOL_CALL_RESULT"), kinds.index("RUN_FINISHED"))
        self.assertEqual(stream[-1]["outcome"], {"type": "success"})
        self.assertEqual(stream[-1]["runId"], payload["runId"])
        run = self.snapshot(stream)
        self.assertEqual(run["status"], "completed")
        tools = [entry for entry in run["workflow"] if entry["kind"] == "tool"]
        self.assertEqual(tools[0]["tool_name"], "agent_users_user_self_read")
        self.assertEqual(tools[0]["status"], "succeeded")
        restored = self.client.get(
            f"/api/v1/chat/runs/{run['id']}", headers=self.headers
        ).json()["data"]
        self.assertEqual(restored["workflow"], run["workflow"])

    def test_native_interrupt_resume_and_duplicate_submission_execute_once(self):
        start = self.call(self.input())
        self.assertEqual(start[-1]["outcome"]["type"], "interrupt")
        run = self.snapshot(start)
        self.assertEqual(run["workflow"][-1]["status"], "waiting")
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).status_code,
            404,
        )
        payload = self.continuation(
            run, values={"/body/display_name": "AG-UI 确认角色"}
        )
        completed = self.call(payload)
        self.assertEqual(self.snapshot(completed)["status"], "completed")
        submitted_arguments = [
            json.loads(event["delta"])
            for event in completed
            if event["type"] == "TOOL_CALL_ARGS"
        ]
        self.assertEqual(
            submitted_arguments[0]["body"]["display_name"], "AG-UI 确认角色"
        )
        self.assertEqual(self.call(payload)[-1]["outcome"]["type"], "success")
        role = self.client.get("/api/v1/roles/new_role", headers=self.headers).json()[
            "data"
        ]
        self.assertEqual(role["version"], 1)
        self.assertEqual(role["display_name"], "AG-UI 确认角色")

    def test_model_exposed_reasoning_is_separate_from_answer(self):
        self.model = SummaryModel(operation="user.self.read", arguments={})
        stream = self.call(self.input())
        summaries = [
            item["delta"]
            for item in stream
            if item["type"] == "REASONING_MESSAGE_CONTENT"
        ]
        self.assertTrue(summaries)
        self.assertIn("核对当前账号", "".join(summaries))
        self.assertNotIn("核对当前账号", self.snapshot(stream)["content"])
        run = self.snapshot(stream)
        discovery = "先查找工具。"
        preface = discovery + "正在查询🔎。"
        self.assertEqual(run["content"], preface + "操作已完成")
        self.assertEqual(
            [(entry["kind"], entry["answer_offset"]) for entry in run["workflow"]],
            [
                ("step", 0),
                ("model", 0),
                ("step", len(discovery)),
                ("model", len(discovery)),
                ("tool", len(preface)),
                ("model", len(preface)),
            ],
        )
        stored = self.client.get(
            f"/api/v1/chat/runs/{run['id']}", headers=self.headers
        ).json()["data"]
        self.assertEqual(stored["workflow"], run["workflow"])
        self.assertEqual(
            sum(item["type"] == "REASONING_MESSAGE_START" for item in stream),
            sum(item["type"] == "REASONING_MESSAGE_END" for item in stream),
        )

    def test_empty_or_encrypted_reasoning_never_becomes_display_text(self):
        self.assertEqual(
            reasoning_text(
                AIMessage(
                    content=[
                        {
                            "type": "reasoning",
                            "summary": None,
                            "encrypted_content": "opaque-provider-value",
                        }
                    ]
                )
            ),
            "",
        )

    def test_sensitive_form_values_never_enter_events_or_workflow(self):
        self.model.operation, self.model.arguments, self.model.prepare = (
            "user.create",
            {
                "body": {
                    "username": "agui_user",
                    "display_name": "协议用户",
                    "password": "model-proposed-secret",
                }
            },
            True,
        )
        run = self.snapshot(self.call(self.input()))
        payload = self.continuation(
            run, sensitive_values={"/body/password": "form-only-password"}
        )
        stream = self.call(payload)
        public = json.dumps(stream, ensure_ascii=False)
        for secret in (
            "form-only-password",
            "model-proposed-secret",
            self.headers["Authorization"],
        ):
            self.assertNotIn(secret, public)
        self.assertEqual(self.snapshot(stream)["status"], "completed")
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "agui_user", "password": "form-only-password"},
        )
        self.assertEqual(login.status_code, 200)

    def test_revoked_grant_and_wrong_thread_cannot_resume_write(self):
        run = self.snapshot(self.call(self.input()))
        payload = self.continuation(run)
        wrong_thread = {**payload, "threadId": "different-thread"}
        self.assertEqual(
            self.client.post(
                "/api/v1/ag-ui", headers=self.headers, json=wrong_thread
            ).status_code,
            409,
        )
        grant_tools(
            self.client, self.headers, self.user_id, ["role.create"], enabled=False
        )
        result = self.client.post("/api/v1/ag-ui", headers=self.headers, json=payload)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.json()["error"]["code"], "TOOL_NOT_GRANTED")
        self.assertEqual(
            self.client.get("/api/v1/roles/new_role", headers=self.headers).status_code,
            404,
        )

    def test_authentication_and_client_tool_injection(self):
        self.assertEqual(
            self.client.post("/api/v1/ag-ui", json=self.input()).status_code, 401
        )
        payload = self.input()
        payload["tools"] = [
            {"name": "admin_bypass", "description": "untrusted tool", "parameters": {}}
        ]
        self.assertEqual(
            self.client.post(
                "/api/v1/ag-ui", headers=self.headers, json=payload
            ).status_code,
            422,
        )

    def test_native_recovery_preserves_completed_tools_and_records_a_new_model_attempt(
        self,
    ):
        self.model.operation, self.model.arguments, self.model.fail_after_tool = (
            "user.self.read",
            {},
            True,
        )
        failed = self.snapshot(self.call(self.input()))
        self.model.fail_after_tool = False
        payload = self.input(
            thread=failed["conversation_id"],
            action="recover",
            businessRunId=failed["id"],
        )
        payload["messages"] = []
        completed = self.snapshot(self.call(payload))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(completed["executions"]), 1)
        self.assertTrue(
            any(
                entry["kind"] == "model" and entry["status"] == "failed"
                for entry in completed["workflow"]
            )
        )
        self.assertTrue(
            any(
                entry["kind"] == "model" and entry["status"] == "succeeded"
                for entry in completed["workflow"]
            )
        )

    def test_invalid_native_resume_is_rejected_without_echoing_values(self):
        run = self.snapshot(self.call(self.input()))
        payload = self.continuation(run)
        payload["resume"][0]["payload"].pop("expected_version")
        payload["resume"][0]["payload"]["sensitive_values"] = {
            "/body/password": "request-only-secret"
        }
        response = self.client.post("/api/v1/ag-ui", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("request-only-secret", response.text)

    def test_lease_recovery_closes_the_previous_model_attempt(self):
        store = self.app.state.run_store
        run, _ = store.create(self.user_id, "lease-recovery", uuid4().hex, {})
        store.record_workflow(
            run,
            {
                "id": "model:previous",
                "kind": "model",
                "title": "模型处理中",
                "status": "running",
            },
        )
        with self.app.state.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE agent_chat_runs SET lease_until=0 WHERE id=?", (run["id"],)
            )
        recovered, fresh = store.recover(run["id"], self.user_id)
        self.assertTrue(fresh)
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(store.workflow(recovered)[0]["status"], "interrupted")

    def test_recovery_restores_the_pending_tool_to_waiting(self):
        run = self.snapshot(self.call(self.input()))
        store = self.app.state.run_store
        with self.app.state.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE agent_chat_runs SET status='interrupted' WHERE id=?",
                (run["id"],),
            )
            store.close_workflow(connection, run["id"], "interrupted")
        payload = self.input(
            thread=run["conversation_id"], action="recover", businessRunId=run["id"]
        )
        restored = self.snapshot(self.call(payload))
        self.assertEqual(restored["status"], "waiting")
        self.assertEqual(restored["workflow"][-1]["status"], "waiting")
        self.assertEqual(restored["interaction"]["id"], run["interaction"]["id"])
        self.assertEqual(
            self.snapshot(self.call(self.continuation(restored)))["status"], "completed"
        )

    def test_schema_four_upgrade_preserves_pending_run_and_backfills_progress_on_resume(
        self,
    ):
        if self.app.state.settings.database_url:
            self.skipTest(
                "SQLite version marker; PostgreSQL migrations have separate coverage"
            )
        run = self.snapshot(self.call(self.input()))
        with self.app.state.database.transaction(write=True) as connection:
            connection.execute("DROP TABLE agent_workflow_entries")
            connection.execute("PRAGMA user_version=4")
        self.app.state.database.initialize()
        response = self.client.get(
            f"/api/v1/chat/runs/{run['id']}", headers=self.headers
        )
        restored = response.json()["data"]
        self.assertEqual(restored["interaction"]["id"], run["interaction"]["id"])
        completed = self.snapshot(self.call(self.continuation(restored)))
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(
            any(
                entry["kind"] == "tool" and entry["status"] == "succeeded"
                for entry in completed["workflow"]
            )
        )

    def test_provider_error_is_a_safe_terminal_event_with_failed_model_step(self):
        self.model.operation, self.model.arguments, self.model.fail_after_tool = (
            "user.self.read",
            {},
            True,
        )
        stream = self.call(self.input())
        self.assertEqual(stream[-1]["type"], "RUN_ERROR")
        self.assertNotIn("offline-failure", json.dumps(stream))
        run = self.snapshot(stream)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["workflow"][-1]["kind"], "model")
        self.assertEqual(run["workflow"][-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
