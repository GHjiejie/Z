"""Account model usage through the Responses, AG-UI and HITL entry points."""

import json
import unittest
from datetime import UTC, datetime, timedelta

from agent_permissions import test_agui, test_hitl
from agent_permissions.test_agui import FailingResponsesStream
from agent_permissions.test_hitl import responses_sse


def provider_response(
    response_id: str,
    input_tokens: int,
    output_tokens: int,
    *,
    tool: str | None = None,
    arguments: dict | None = None,
) -> bytes:
    output = (
        {
            "type": "function_call",
            "id": "fc_" + response_id,
            "call_id": "call_" + response_id,
            "status": "completed",
            "name": tool,
            "arguments": json.dumps(arguments or {}),
        }
        if tool
        else {
            "type": "message",
            "id": "msg_" + response_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "已完成", "annotations": []}],
        }
    )
    return responses_sse(
        {
            "id": "resp_" + response_id,
            "object": "response",
            "model": "stream-fixture",
            "created_at": 1,
            "status": "completed",
            "output": [output],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    ).content


class ModelUsageTests(unittest.TestCase):
    settings_for = test_hitl.HitlTests.settings_for
    application = test_hitl.HitlTests.application
    setUp = test_hitl.HitlTests.setUp
    start = test_hitl.HitlTests.start
    submit = test_hitl.HitlTests.submit
    final = test_hitl.HitlTests.final
    snapshot = test_hitl.HitlTests.snapshot
    input = test_agui.AguiTests.input
    call = test_agui.AguiTests.call
    scripted_responses = test_agui.AguiTests.scripted_responses

    def login(self, username="admin") -> dict[str, str]:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "test-password"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["data"]["session_token"]}

    def report(self, user_id=None, *, headers=None, **params) -> dict:
        response = self.client.get(
            f"/api/v1/users/{user_id or self.user_id}/token-usage",
            headers=self.headers if headers is None else headers,
            params=params,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_provider_rounds_keep_confirmed_usage_when_final_round_times_out(self):
        requests = self.scripted_responses(
            provider_response(
                "find",
                300,
                25,
                tool="search_tools",
                arguments={"query": "user.self.read"},
            ),
            provider_response("read", 450, 20, tool="agent_users_user_self_read"),
            FailingResponsesStream(),
        )
        stream = self.call(self.input())
        self.assertEqual(stream[-1]["code"], "chat_timeout")
        self.assertEqual(len(requests), 3)
        summary = self.report()
        self.assertEqual(summary["input_tokens"], 750)
        self.assertEqual(summary["output_tokens"], 45)
        self.assertEqual(summary["total_tokens"], 795)
        self.assertEqual(summary["cached_input_tokens"], 10)
        self.assertEqual(summary["reasoning_tokens"], 4)
        self.assertEqual(summary["model_calls"], 3)
        self.assertEqual(summary["unknown_calls"], 1)

    def test_first_round_retry_is_a_separate_attempt(self):
        requests = self.scripted_responses(
            FailingResponsesStream(), provider_response("retry", 120, 18)
        )
        self.assertEqual(self.call(self.input())[-1]["type"], "RUN_FINISHED")
        self.assertEqual(len(requests), 2)
        summary = self.report()
        self.assertEqual(summary["total_tokens"], 138)
        self.assertEqual(summary["model_calls"], 2)
        self.assertEqual(summary["unknown_calls"], 1)

    def test_usage_in_terminal_frame_is_saved_even_if_the_stream_fails_after_it(self):
        self.scripted_responses(
            FailingResponsesStream(prefix=provider_response("done", 16, 4))
        )
        self.assertEqual(self.call(self.input())[-1]["code"], "chat_timeout")
        summary = self.report()
        self.assertEqual(summary["total_tokens"], 20)
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["unknown_calls"], 0)

    def test_usage_is_attributed_to_the_authenticated_account_and_authorized_on_read(
        self,
    ):
        response = self.client.post(
            "/api/v1/users",
            headers=self.headers,
            json={
                "username": "usage_member",
                "display_name": "用量测试",
                "password": "test-password",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        member_id = response.json()["data"]["id"]
        member = self.login("usage_member")
        self.scripted_responses(provider_response("member", 42, 7))
        response = self.client.post(
            "/v1/chat/completions",
            headers=member,
            json={
                "model": "stream-fixture",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage"]["total_tokens"], 49)
        self.assertIsNone(self.report()["total_tokens"])
        own = self.report(member_id, headers=member)
        self.assertEqual(own["total_tokens"], 49)
        self.assertEqual(own, self.report(member_id))
        path = f"/api/v1/users/{self.user_id}/token-usage"
        self.assertEqual(self.client.get(path, headers=member).status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/v1/users/unknown/token-usage", headers=self.headers
            ).status_code,
            404,
        )

    def test_date_range_includes_both_days_at_beijing_boundaries(self):
        self.now = datetime(2026, 11, 17, 15, 59, 59, tzinfo=UTC)
        self.headers = self.login()
        self.scripted_responses(
            provider_response("day-one", 30, 5), provider_response("day-two", 300, 15)
        )
        self.call(self.input())
        self.now += timedelta(seconds=1)
        self.call(self.input())
        self.assertEqual(
            self.report(from_date="2026-11-17", to_date="2026-11-17")["total_tokens"],
            35,
        )
        self.assertEqual(self.report(from_date="2026-11-18")["total_tokens"], 315)
        self.assertEqual(self.report(to_date="2026-11-18")["total_tokens"], 350)
        self.assertEqual(
            self.client.get(
                f"/api/v1/users/{self.user_id}/token-usage",
                headers=self.headers,
                params={"from_date": "2026-11-18", "to_date": "2026-11-17"},
            ).status_code,
            422,
        )

    def test_legacy_waiting_run_is_imported_once_and_resume_only_counts_new_calls(self):
        run = self.start()
        prior = run["usage"]["total_tokens"]
        database = self.app.state.database
        with database.transaction(write=True) as connection:
            connection.execute("DROP TABLE agent_model_usage")
            if self.settings.database_url:
                connection.execute(
                    "UPDATE schema_metadata SET version=6 WHERE key='schema_version'"
                )
            else:
                connection.execute("PRAGMA user_version=6")
        database.initialize()
        database.initialize()
        summary = self.report()
        self.assertEqual(summary["legacy_runs"], 1)
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["total_tokens"], prior)

        done = self.final(self.submit(run))
        self.assertEqual(done["status"], "completed")
        self.final(self.submit(run))
        summary = self.report()
        self.assertEqual(summary["total_tokens"], done["usage"]["total_tokens"])
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["legacy_runs"], 1)

        self.now += timedelta(days=2)
        self.headers = self.login()
        self.assertTrue(self.snapshot(run)["purged"])
        self.assertEqual(self.report(), summary)
