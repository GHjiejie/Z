"""OpenAI SDK acceptance against a real Agent and isolated business database."""

import asyncio
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

import httpx
import openai
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field
from starlette.requests import Request

from agent_permissions.api import create_app
from agent_permissions.chat_api import ChatRequest, _stream
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.service import RequestInfo
from agent_permissions.test_support import grant_tools
from agent_permissions.tracing import ChatTracing, TracingSettings

USAGE = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}


class BusinessModel(BaseChatModel):
    model_name: str = "fixture-model"
    mode: str = "profile"
    tool_names: list[list[str]] = Field(default_factory=list)
    on_first_call: Callable | None = None
    failure: Exception | None = None
    options: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offline-business-agent"

    def bind_tools(self, tools, **kwargs):
        return self.bind(bound_names=[tool.name for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.tool_names.append(kwargs.get("bound_names", []))
        self.options.append(kwargs)
        if self.failure:
            raise self.failure
        if self.mode == "echo":
            response = AIMessage(content=messages[-1].content, usage_metadata=USAGE)
        elif isinstance(messages[-1], ToolMessage):
            tool = messages[-1]
            if tool.status == "error":
                try:
                    text = json.loads(tool.content)["error"]["code"]
                except (ValueError, KeyError):
                    text = "PERMISSION_DENIED"
            else:
                result = json.loads(tool.content)
                if "error" in result:
                    text = result["error"]["code"]
                elif self.mode == "create":
                    text = "created:" + result["body"]["data"]["code"]
                else:
                    text = result["body"]["data"]["username"]
            response = AIMessage(content=text, usage_metadata=USAGE)
        else:
            if self.on_first_call:
                self.on_first_call()
            name, arguments = "agent_users_user_self_read", {}
            if self.mode == "create":
                name = "agent_users_role_create"
                arguments = {
                    "body": {
                        "code": "chat_role",
                        "display_name": "聊天角色",
                        "permissions": [],
                    }
                }
            elif self.mode == "list":
                name = "agent_users_user_list"
            response = AIMessage(
                content="",
                tool_calls=[{"id": "call_fixture", "name": name, "args": arguments}],
                usage_metadata=USAGE,
            )
        return ChatResult(generations=[ChatGeneration(message=response)])


class OpenAIChatTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.model = BusinessModel()
        self.factory_calls = 0

        def factory():
            self.factory_calls += 1
            return self.model

        self.app = create_app(
            Settings(database_path=Path(directory) / "chat.sqlite3"),
            chat_model_factory=factory,
            chat_tracing=ChatTracing(TracingSettings()),
        )
        self.http = self.enterContext(TestClient(self.app))
        self.app.state.service.bootstrap_admin(
            UserCreate(
                username="admin", display_name="管理员", password="offline-password"
            )
        )
        self.token = self.login("admin")
        admin_headers = {"Authorization": "Bearer " + self.token}
        user_id = self.http.get("/api/v1/users/me", headers=admin_headers).json()[
            "data"
        ]["id"]
        grant_tools(self.http, admin_headers, user_id)
        self.sdk = self.sdk_for(self.token)

    def login(self, username):
        response = self.http.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "offline-password"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["session_token"]

    def sdk_for(self, token):
        return openai.OpenAI(
            base_url="http://testserver/v1",
            api_key=token,
            http_client=self.http,
            max_retries=0,
        )

    def create_member(self):
        response = self.http.post(
            "/api/v1/users",
            headers={"Authorization": "Bearer " + self.token},
            json={
                "username": "member",
                "display_name": "成员",
                "password": "offline-password",
            },
        )
        self.assertEqual(response.status_code, 201)
        grant_tools(
            self.http,
            {"Authorization": "Bearer " + self.token},
            response.json()["data"]["id"],
        )
        return self.login("member")

    def test_sync_sdk_runs_real_tool_and_decodes_standard_completion(self) -> None:
        self.assertEqual(self.sdk.models.list().data[0].id, "fixture-model")
        result = self.sdk.chat.completions.create(
            model="fixture-model",
            messages=[{"role": "user", "content": "查看我的账号"}],
            temperature=0.2,
            max_completion_tokens=256,
        )
        self.assertEqual(result.object, "chat.completion")
        self.assertEqual(result.choices[0].message.content, "admin")
        self.assertEqual(result.choices[0].finish_reason, "stop")
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertEqual(result.usage.total_tokens, 24)
        self.assertTrue(result._request_id.startswith("req_"))
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual(self.model.options[-1]["max_tokens"], 256)
        self.assertEqual(self.model.options[-1]["temperature"], 0.2)
        self.assertNotIn("agent_users_auth_login", self.model.tool_names[0])

    def test_stream_sdk_decodes_deltas_finish_and_usage(self) -> None:
        stream = self.sdk.chat.completions.create(
            model="fixture-model",
            messages=[{"role": "user", "content": "查看我的账号"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = list(stream)
        self.assertEqual(chunks[0].choices[0].delta.role, "assistant")
        self.assertEqual(
            "".join(
                choice.delta.content or "" for item in chunks for choice in item.choices
            ),
            "admin",
        )
        self.assertEqual(chunks[-2].choices[0].finish_reason, "stop")
        self.assertEqual(chunks[-1].choices, [])
        self.assertEqual(chunks[-1].usage.total_tokens, 24)
        self.assertEqual(len({chunk.id for chunk in chunks}), 1)
        self.assertEqual({chunk.object for chunk in chunks}, {"chat.completion.chunk"})
        raw = self.http.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.token},
            json={
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "查看账号"}],
                "stream": True,
            },
        )
        self.assertTrue(raw.headers["content-type"].startswith("text/event-stream"))
        self.assertTrue(raw.text.endswith("data: [DONE]\n\n"))
        self.assertNotIn('"tool_calls"', raw.text)

    def test_async_sdk_and_concurrent_users_keep_separate_credentials(self) -> None:
        member = self.create_member()

        async def exercise():
            async def call(token):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                ) as http:
                    sdk = openai.AsyncOpenAI(
                        base_url="http://testserver/v1",
                        api_key=token,
                        http_client=http,
                        max_retries=0,
                    )
                    result = await sdk.chat.completions.create(
                        model="fixture-model",
                        messages=[{"role": "user", "content": "我是谁"}],
                    )
                    return result.choices[0].message.content

            self.assertEqual(
                await asyncio.gather(call(self.token), call(member)),
                ["admin", "member"],
            )

        asyncio.run(exercise())

    def test_agent_can_write_only_for_authorized_user(self) -> None:
        self.model.mode = "create"
        member = self.create_member()
        denied = self.sdk_for(member).chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "创建角色"}]
        )
        self.assertEqual(denied.choices[0].message.content, "PERMISSION_DENIED")
        with self.app.state.database.transaction() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT code FROM roles WHERE code='chat_role'"
                ).fetchone()
            )
        result = self.sdk.chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "创建角色"}]
        )
        self.assertIn("尚未执行", result.choices[0].message.content)
        with self.app.state.database.transaction() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT code FROM roles WHERE code='chat_role'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM audit_events WHERE action='role.create'"
                ).fetchone()
            )

    def test_revocation_during_agent_run_blocks_already_discovered_tool(self) -> None:
        headers = {"Authorization": "Bearer " + self.token}
        self.http.post(
            "/api/v1/roles",
            headers=headers,
            json={
                "code": "viewer",
                "display_name": "查看者",
                "permissions": ["user.read"],
            },
        )
        self.http.post(
            "/api/v1/users",
            headers=headers,
            json={
                "username": "viewer",
                "display_name": "查看者",
                "password": "offline-password",
                "roles": ["viewer"],
            },
        )
        viewer = self.login("viewer")
        viewer_id = self.http.get(
            "/api/v1/users", headers=headers, params={"q": "viewer"}
        ).json()["data"]["items"][0]["id"]
        grant_tools(self.http, headers, viewer_id, ["user.list"])
        self.model.mode = "list"

        def revoke():
            with self.app.state.database.transaction(write=True) as connection:
                connection.execute(
                    "DELETE FROM role_permissions WHERE role_code='viewer'"
                )

        self.model.on_first_call = revoke
        result = self.sdk_for(viewer).chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "列出用户"}]
        )
        self.assertEqual(result.choices[0].message.content, "PERMISSION_DENIED")
        self.assertIn("agent_users_user_list", self.model.tool_names[0])

    def test_no_delegated_tools_still_supports_normal_and_streaming_chat(self):
        headers = {"Authorization": "Bearer " + self.token}
        user_id = self.http.get("/api/v1/users/me", headers=headers).json()["data"][
            "id"
        ]
        endpoint = f"/api/v1/users/{user_id}/agent-tools"
        view = self.http.get(endpoint, headers=headers).json()["data"]
        self.http.patch(
            endpoint,
            headers=headers,
            json={
                "expected_version": view["version"],
                "catalog_revision": view["catalog_revision"],
                "changes": [
                    {"operation_id": item["operation_id"], "enabled": False}
                    for item in view["items"]
                ],
            },
        ).raise_for_status()
        self.model.mode = "echo"
        result = self.sdk.chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "你好"}]
        )
        self.assertEqual(result.choices[0].message.content, "你好")
        chunks = list(
            self.sdk.chat.completions.create(
                model="fixture-model",
                messages=[{"role": "user", "content": "你好"}],
                stream=True,
            )
        )
        self.assertEqual(
            "".join(
                chunk.choices[0].delta.content or ""
                for chunk in chunks
                if chunk.choices
            ),
            "你好",
        )
        self.assertTrue(all(not names for names in self.model.tool_names))

    def test_delegation_revocation_during_agent_run_blocks_cached_tool(self):
        headers = {"Authorization": "Bearer " + self.token}
        user_id = self.http.get("/api/v1/users/me", headers=headers).json()["data"][
            "id"
        ]

        def revoke():
            with self.app.state.database.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE user_agent_tool_grants SET enabled=0 WHERE user_id=? AND operation_id='user.self.read'",
                    (user_id,),
                )

        self.model.on_first_call = revoke
        result = self.sdk.chat.completions.create(
            model="fixture-model", messages=[{"role": "user", "content": "查看资料"}]
        )
        self.assertEqual(result.choices[0].message.content, "TOOL_NOT_GRANTED")

    def test_sdk_authentication_validation_and_unknown_model_errors(self) -> None:
        with self.assertRaises(openai.AuthenticationError) as caught:
            self.sdk_for("invalid-session").models.list()
        self.assertEqual(caught.exception.body["type"], "authentication_error")
        self.assertEqual(self.factory_calls, 0)
        with self.assertRaises(openai.NotFoundError) as caught:
            self.sdk.chat.completions.create(
                model="unknown", messages=[{"role": "user", "content": "hello"}]
            )
        self.assertEqual(caught.exception.body["code"], "model_not_found")
        self.assertEqual(caught.exception.body["param"], "model")
        for extra in (
            {"n": 2},
            {"tools": []},
            {"stream": "true"},
            {"stream_options": {"include_usage": True}},
            {"max_tokens": 5, "max_completion_tokens": 5},
        ):
            with self.subTest(extra=extra), self.assertRaises(openai.BadRequestError):
                self.sdk.chat.completions.create(
                    model="fixture-model",
                    messages=[{"role": "user", "content": "hello"}],
                    extra_body=extra,
                )
        self.assertEqual(self.model.tool_names, [])

    def test_text_parts_developer_history_and_explicit_no_tools(self) -> None:
        self.model.mode = "echo"
        result = self.sdk.chat.completions.create(
            model="fixture-model",
            messages=[
                {"role": "developer", "content": "Be concise"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "text", "text": " world"},
                    ],
                },
            ],
            tool_choice="none",
        )
        self.assertEqual(result.choices[0].message.content, "hello world")
        self.assertEqual(self.model.tool_names, [[]])

    def test_model_error_and_stream_error_are_redacted_for_sdk(self) -> None:
        secret = "private-provider-credential-and-input"
        self.model.failure = RuntimeError(secret)
        with self.assertRaises(openai.InternalServerError) as caught:
            self.sdk.chat.completions.create(
                model="fixture-model", messages=[{"role": "user", "content": "hello"}]
            )
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(openai.APIError) as caught:
            list(
                self.sdk.chat.completions.create(
                    model="fixture-model",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=True,
                )
            )
        self.assertNotIn(secret, str(caught.exception))
        raw = self.http.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer " + self.token},
            json={
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        self.assertNotIn("[DONE]", raw.text)
        self.assertNotIn(secret, raw.text)

    def test_missing_model_configuration_is_a_safe_service_unavailable_error(
        self,
    ) -> None:
        def unavailable():
            raise ValueError("private-provider-key")

        self.app.state.chat_model_factory = unavailable
        with self.assertRaises(openai.APIStatusError) as caught:
            self.sdk.models.list()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private-provider-key", str(caught.exception))

    def test_closing_stream_aborts_graph_and_releases_agent_context(self) -> None:
        class SlowModel(BusinessModel):
            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                yield ChatGenerationChunk(message=AIMessageChunk(content="partial"))
                await asyncio.sleep(10)

        async def exercise():
            request = Request(
                {
                    "type": "http",
                    "app": self.app,
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "query_string": b"",
                    "headers": [(b"authorization", ("Bearer " + self.token).encode())],
                    "client": ("test", 0),
                    "state": {"info": RequestInfo("abort", "test")},
                }
            )
            stream = _stream(
                request,
                ChatRequest(
                    model="fixture-model",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=True,
                ),
                SlowModel(),
                {"id": "abort", "created": 1, "model": "fixture-model"},
            )
            self.assertIn('"role": "assistant"', await anext(stream))
            self.assertIn("partial", await anext(stream))
            await stream.aclose()

        asyncio.run(exercise())
        with self.app.state.database.transaction() as connection:
            run = connection.execute(
                "SELECT status,output FROM agent_chat_runs"
            ).fetchone()
            self.assertEqual(tuple(run), ("interrupted", "partial"))


if __name__ == "__main__":
    unittest.main()
