"""Real LangSmith serialization and Agent spans with an offline trace transport."""

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import requests
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langsmith import Client
from langsmith.run_helpers import get_tracing_context
from langsmith.schemas import LangSmithInfo
from pydantic import SecretStr
from starlette.requests import Request

from agent_permissions.api import create_app
from agent_permissions.chat_api import ChatRequest, _stream
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.service import RequestInfo
from agent_permissions.test_chat_api import BusinessModel
from agent_permissions.test_support import grant_tools
from agent_permissions.tracing import ChatTracing, TracingSettings, redact_trace


class RecordingSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.runs: list[dict] = []

    def request(self, method, url, *args, **kwargs):
        if kwargs.get("data"):
            self.runs.append({"method": method.upper(), **json.loads(kwargs["data"])})
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.url = url
        return response


def offline_client(session: RecordingSession, **kwargs) -> Client:
    return Client(
        api_url="https://smith.example.test",
        api_key="offline-langsmith-key",
        session=session,
        auto_batch_tracing=False,
        info=LangSmithInfo(),
        anonymizer=redact_trace,
        omit_traced_runtime_info=True,
        disable_prompt_cache=True,
        **kwargs,
    )


class TracingConfigurationTests(unittest.TestCase):
    def test_local_configuration_does_not_export_or_read_shared_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=local-test-key\n"
                "LANGSMITH_PROJECT=local-project\nLANGSMITH_HIDE_INPUTS=true\n"
            )
            with patch.dict(
                os.environ, {"LANGCHAIN_API_KEY": "shared-key"}, clear=True
            ):
                before = dict(os.environ)
                settings = TracingSettings.from_env(path)
                self.assertTrue(settings.enabled)
                self.assertEqual(settings.api_key.get_secret_value(), "local-test-key")
                self.assertEqual(settings.project, "local-project")
                self.assertTrue(settings.hide_inputs)
                self.assertEqual(dict(os.environ), before)
                self.assertNotIn("local-test-key", repr(settings))
                with patch.dict(os.environ, {"LANGSMITH_PROJECT": "process-project"}):
                    self.assertEqual(
                        TracingSettings.from_env(path).project, "process-project"
                    )

    def test_missing_key_and_invalid_flags_are_explicit_and_app_can_disable(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(directory) / ".env"
            path.write_text("LANGSMITH_TRACING=true\n")
            with self.assertRaisesRegex(ValueError, "LANGSMITH_API_KEY"):
                TracingSettings.from_env(path)
            path.write_text("LANGSMITH_TRACING=invalid\n")
            with (
                patch("agent_permissions.tracing.DEFAULT_ENV_FILE", path),
                self.assertLogs("agent_permissions.tracing"),
            ):
                self.assertFalse(ChatTracing.from_env().settings.enabled)

    def test_disabled_context_overrides_shared_tracing_and_does_not_create_client(
        self,
    ) -> None:
        tracing = ChatTracing(TracingSettings())
        before = get_tracing_context()
        with (
            patch.dict(os.environ, {"LANGSMITH_TRACING": "true"}),
            patch("agent_permissions.tracing.Client") as client,
        ):
            with tracing.context(
                request_id="disabled", model="test", stream=False, tools=[]
            ):
                self.assertFalse(get_tracing_context()["enabled"])
            client.assert_not_called()
        self.assertEqual(get_tracing_context(), before)

    def test_unavailable_tracing_client_keeps_business_execution_available(
        self,
    ) -> None:
        tracing = ChatTracing(
            TracingSettings(enabled=True, api_key=SecretStr("test-key"))
        )
        with (
            patch(
                "agent_permissions.tracing.Client",
                side_effect=RuntimeError("private-key"),
            ),
            self.assertLogs("agent_permissions.tracing") as logs,
            tracing.context(request_id="failure", model="test", stream=False, tools=[]),
        ):
            self.assertFalse(get_tracing_context()["enabled"])
        self.assertNotIn("private-key", " ".join(logs.output))

    def test_structured_credentials_json_tool_messages_and_errors_are_masked(
        self,
    ) -> None:
        session = RecordingSession()
        client = offline_client(session)
        try:
            identity = uuid4()
            inputs = {
                "body": {"password": "private-password", "username": "visible"},
                "model_blocks": [
                    {
                        "type": "function_call",
                        "name": "example",
                        "arguments": '{"pin":"private-proposal"}',
                    },
                    {
                        "type": "tool_call",
                        "name": "example",
                        "args": {"pin": "private-proposal"},
                    },
                    {"type": "tool_call_chunk", "args": "private-fragment"},
                    {
                        "type": "invalid_tool_call",
                        "args": "private-invalid",
                        "error": "private-diagnostic",
                    },
                ],
                "content": json.dumps(
                    {"body": {"new_password": "private-new-password"}}
                ),
                "headers": {"Authorization": "Bearer private-session-token"},
            }
            client.create_run("redaction", inputs, "tool", id=identity)
            client.update_run(
                identity,
                outputs={"body": {"session_token": "private-session-token"}},
                error="failed with sk-" + "a" * 40,
            )
            recorded = json.dumps(session.runs)
            for secret in (
                "private-password",
                "private-new-password",
                "private-session-token",
                "private-proposal",
                "private-fragment",
                "private-invalid",
                "private-diagnostic",
                "sk-" + "a" * 40,
            ):
                self.assertNotIn(secret, recorded)
            self.assertIn("visible", recorded)
            self.assertEqual(inputs["body"]["password"], "private-password")
        finally:
            client.close(timeout=0)

    def test_hide_flags_remove_entire_content(self) -> None:
        session = RecordingSession()
        client = offline_client(session, hide_inputs=True, hide_outputs=True)
        try:
            identity = uuid4()
            client.create_run(
                "hidden", {"prompt": "private-prompt"}, "chain", id=identity
            )
            client.update_run(identity, outputs={"answer": "private-answer"})
            self.assertNotIn("private-prompt", json.dumps(session.runs))
            self.assertNotIn("private-answer", json.dumps(session.runs))
        finally:
            client.close(timeout=0)


class ChatTracingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(redirect_stdout(io.StringIO()))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.session = RecordingSession()
        self.client = offline_client(self.session)
        self.model = BusinessModel()
        self.tracing = ChatTracing(
            TracingSettings(enabled=True, api_key=SecretStr("offline-key")), self.client
        )
        self.app = create_app(
            Settings(database_path=Path(directory) / "tracing.sqlite3"),
            chat_model_factory=lambda: self.model,
            chat_tracing=self.tracing,
        )
        self.http = self.enterContext(TestClient(self.app))
        self.app.state.service.bootstrap_admin(
            UserCreate(
                username="admin", display_name="管理员", password="offline-password"
            )
        )
        login = self.http.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "offline-password"},
        )
        self.token = login.json()["data"]["session_token"]
        grant_tools(
            self.http,
            {"Authorization": "Bearer " + self.token},
            login.json()["data"]["user"]["id"],
        )

    def chat(self, stream=False, **options):
        return self.http.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.token},
            json={
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "查看账号"}],
                "stream": stream,
                **options,
            },
        )

    def roots(self):
        return [
            run
            for run in self.session.runs
            if run["method"] == "POST" and run.get("name") == "agent_permissions.chat"
        ]

    def test_normal_and_sse_requests_record_graph_models_and_tools(self) -> None:
        for stream in (False, True):
            response = self.chat(stream)
            self.assertEqual(response.status_code, 200, response.text)
            if stream:
                self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
            root = self.roots()[-1]
            metadata = root["extra"]["metadata"]
            self.assertEqual(metadata["request_id"], response.headers["X-Request-ID"])
            self.assertEqual(metadata["tool_count"], 20)
            self.assertEqual(metadata["stream"], stream)
            children = [
                run
                for run in self.session.runs
                if run.get("trace_id") == root["trace_id"]
            ]
            self.assertTrue(any(run.get("run_type") == "llm" for run in children))
            self.assertTrue(
                any(run.get("name") == "agent_users_user_self_read" for run in children)
            )
            self.assertTrue(
                any(
                    run["method"] == "PATCH"
                    and run["id"] == root["id"]
                    and run.get("end_time")
                    for run in children
                )
            )
        self.assertEqual(len({root["trace_id"] for root in self.roots()}), 2)
        self.assertNotIn(self.token, json.dumps(self.session.runs))

    def test_form_secret_is_absent_from_langsmith_and_resumed_traces_share_run_id(self):
        from agent_permissions.test_hitl import InteractionModel, events

        self.app.state.chat_model = InteractionModel(
            operation="user.create", arguments={}, prepare=True
        )
        headers = {"Authorization": "Bearer " + self.token}
        started = self.http.post(
            "/api/v1/chat/runs",
            headers=headers,
            json={
                "model": "hitl-fixture",
                "messages": [{"role": "user", "content": "创建用户"}],
                "conversation_id": "trace-hitl",
                "client_request_id": "trace-hitl",
            },
        )
        run = [event["run"] for event in events(started) if "run" in event][-1]
        secret = "private-hitl-form-value-1234"
        result = self.http.post(
            f"/api/v1/chat/runs/{run['id']}/interactions/{run['interaction']['id']}/resume",
            headers=headers,
            json={
                "submission_id": "trace-submit",
                "expected_version": 1,
                "values": {
                    "/body/username": "trace_user",
                    "/body/display_name": "追踪用户",
                },
                "sensitive_values": {"/body/password": secret},
            },
        )
        done = [event["run"] for event in events(result) if "run" in event][-1]
        self.assertEqual(done["status"], "completed", done)
        self.assertNotIn(secret, json.dumps(self.session.runs))
        self.assertEqual(
            {item["extra"]["metadata"]["run_id"] for item in self.roots()}, {run["id"]}
        )
        self.assertEqual(len(self.roots()), 2)

    def test_concurrent_requests_keep_separate_trace_roots(self) -> None:
        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as http:

                async def request():
                    return await http.post(
                        "/v1/chat/completions",
                        headers={"Authorization": "Bearer " + self.token},
                        json={
                            "model": "fixture-model",
                            "messages": [{"role": "user", "content": "查看账号"}],
                        },
                    )

                return await asyncio.gather(request(), request())

        results = asyncio.run(exercise())
        self.assertEqual([item.status_code for item in results], [200, 200])
        roots = self.roots()
        self.assertEqual(len(roots), 2)
        self.assertEqual(len({run["trace_id"] for run in roots}), 2)
        self.assertEqual(
            {run["extra"]["metadata"]["request_id"] for run in roots},
            {item.headers["X-Request-ID"] for item in results},
        )

    def test_no_tools_and_model_failure_are_still_traced(self) -> None:
        self.model.mode = "echo"
        response = self.chat(tool_choice="none")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.roots()[0]["extra"]["metadata"]["tool_count"], 0)
        model = self.app.state.chat_model
        model.failure = RuntimeError("model failure")
        self.assertEqual(self.chat().status_code, 500)
        failed_root = self.roots()[-1]
        self.assertTrue(
            any(
                run.get("id") == failed_root["id"] and run.get("error")
                for run in self.session.runs
            )
        )

    def test_closing_sse_finishes_trace_and_restores_context(self) -> None:
        class SlowModel(BusinessModel):
            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
                await asyncio.sleep(10)
                yield ChatGenerationChunk(message=AIMessageChunk(content=" finished"))

        async def exercise():
            request = Request(
                {
                    "type": "http",
                    "app": self.app,
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {self.token}".encode())],
                    "client": ("test", 0),
                    "state": {"info": RequestInfo("cancelled-stream", "test")},
                }
            )
            payload = ChatRequest(
                model="fixture-model",
                messages=[{"role": "user", "content": "查看账号"}],
                stream=True,
            )
            before = get_tracing_context()
            stream = _stream(
                request,
                payload,
                SlowModel(),
                {"id": "cancelled", "created": 1, "model": "fixture-model"},
            )
            self.assertIn('"role": "assistant"', await anext(stream))
            self.assertIn('"content": "partial"', await anext(stream))
            await stream.aclose()
            self.assertEqual(get_tracing_context(), before)

        asyncio.run(exercise())
        root = self.roots()[0]
        self.assertTrue(
            any(
                run.get("id") == root["id"] and run.get("end_time")
                for run in self.session.runs
            )
        )


if __name__ == "__main__":
    unittest.main()
