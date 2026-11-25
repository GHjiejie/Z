"""Exercise discovery through the real run API with a scripted Responses-style model."""

import json
import unittest
from math import ceil
from unittest.mock import patch
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agent_permissions import test_hitl
from agent_permissions.agent_grants import configurable_tools
from agent_permissions.hitl_checkpoint import DatabaseCheckpointer
from agent_permissions.hitl_runtime import PREPARE_NAME
from agent_permissions.test_support import grant_tools
from agent_permissions.tool_discovery import (
    CANDIDATE_LIMIT,
    PAGE_SIZE,
    SEARCH_NAME,
    catalog_vectors,
    initial_operations,
    matching_operations,
)
from api_tools.compiler import digest


def search(query: str = "", **options) -> dict:
    return {"name": SEARCH_NAME, "args": {"query": query, **options}}


# Labeled conversation examples for regression comparisons on the same catalog.
INTENT_CASES = [
    ("我想换个昵称", "user.self.update"),
    ("先让这个同事暂时登不进来", "user.status.update"),
    ("我用掉了多少词元", "user.self.token_usage"),
    ("给组里新来的小陈开个账号", "user.create"),
    ("他忘了口令，得重设一下", "user.password.reset"),
    ("我上个月用了多少 token", "user.self.token_usage"),
    ("查询指定用户的模型用量", "user.token_usage"),
    ("列出用户", "user.list"),
    ("创建一个角色", "role.create"),
    ("我的账号信息", "user.self.read"),
]


class ScriptedModel(BaseChatModel):
    model_name: str = "discovery-fixture"
    turns: list[list[dict]] = Field(default_factory=list)
    offered: list[list[str]] = Field(default_factory=list)
    results: list[list[dict]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offline-tool-discovery"

    def bind_tools(self, tools, **kwargs):
        return self.bind(offered=[tool.name for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.offered.append(kwargs.get("offered", []))
        outputs = []
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            outputs.append(json.loads(message.content))
        self.results.append(outputs[::-1])
        calls = [
            {**call, "id": f"call_{len(self.offered)}_{i}"}
            for i, call in enumerate(self.turns.pop(0))
        ]
        reply = AIMessage(content="" if calls else "完成", tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=reply)])


class DiscoveryTests(unittest.TestCase):
    settings_for = test_hitl.HitlTests.settings_for
    application = test_hitl.HitlTests.application
    final = test_hitl.HitlTests.final

    def setUp(self) -> None:
        test_hitl.HitlTests.setUp(self)
        self.model = ScriptedModel()

    def update_catalog_revision(self) -> None:
        registry = self.app.state.tool_registry
        bundle = registry.snapshot()
        bundle["sourceRevision"] = "a" * 64
        bundle["bundleDigest"] = digest(
            {k: v for k, v in bundle.items() if k != "bundleDigest"}
        )
        registry.activate(bundle, expected=bundle)

    def chat(
        self,
        *turns: list[dict],
        headers=None,
        prompt="查询当前账号可用的工具",
        waiting=False,
        **options,
    ) -> dict:
        self.model.turns = [*turns, []]
        self.model.offered.clear()
        self.model.results.clear()
        response = self.client.post(
            "/api/v1/chat/runs",
            headers=headers or self.headers,
            json={
                "model": self.model.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "conversation_id": "discovery",
                "client_request_id": uuid4().hex,
                **options,
            },
        )
        run = self.final(response)
        self.assertEqual(run["status"], "waiting" if waiting else "completed", run)
        self.assertEqual(self.model.turns, [[]] if waiting else [])
        return run

    def test_first_turn_paraphrase_binds_only_the_profile_update_and_requires_confirmation(
        self,
    ):
        update = {
            "name": "agent_users_user_self_update",
            "args": {"body": {"display_name": "新昵称"}},
        }
        run = self.chat([update], prompt="我想换个昵称", waiting=True)
        self.assertEqual(
            self.model.offered, [[SEARCH_NAME, update["name"], PREPARE_NAME]]
        )
        self.assertTrue(run["interaction"]["confirmation_required"])
        self.assertEqual(run["executions"], [])
        profile = "/api/v1/users/me"
        self.assertNotEqual(
            self.client.get(profile, headers=self.headers).json()["data"][
                "display_name"
            ],
            "新昵称",
        )
        completed = self.final(test_hitl.HitlTests.submit(self, run))
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(
            [e["operation_id"] for e in completed["executions"]], ["user.self.update"]
        )
        self.assertEqual(
            self.client.get(profile, headers=self.headers).json()["data"][
                "display_name"
            ],
            "新昵称",
        )

    def test_followup_reads_with_the_prior_user_intent_and_new_topics_start_fresh(self):
        usage = {"name": "agent_users_user_self_token_usage", "args": {}}
        history = [
            {"role": "user", "content": "看看我用了多少词元"},
            {"role": "assistant", "content": "已经查询。"},
            {"role": "user", "content": "那上个月呢？"},
        ]
        run = self.chat([usage], messages=history)
        self.assertEqual(len(self.model.offered), 2)
        self.assertIn(usage["name"], self.model.offered[0])
        self.assertEqual(
            [e["operation_id"] for e in run["executions"]], ["user.self.token_usage"]
        )
        self.assertEqual(
            self.model.results[-1][0]["body"]["data"]["user_id"], self.user_id
        )
        for other in ("你好", "查一下明天的天气", "查询订单"):
            with self.subTest(other=other):
                self.chat(messages=[*history, {"role": "user", "content": other}])
                self.assertEqual(self.model.offered, [[SEARCH_NAME]])
        self.chat(messages=history, tool_choice="none")
        self.assertEqual(self.model.offered, [[]])

    def test_semantic_fallback_replaces_prefetched_ids(self):
        self.chat([search("先让这个同事暂时登不进来")], prompt="我想换个昵称")
        self.assertIn("agent_users_user_self_update", self.model.offered[0])
        self.assertEqual(
            self.model.offered[1],
            [SEARCH_NAME, "agent_users_user_status_update", PREPARE_NAME],
        )

    def test_allowed_candidates_and_mutation_checks_apply_to_first_turn(self):
        snapshot = self.app.state.tool_registry.snapshot()
        tools = configurable_tools(snapshot)
        vectors = catalog_vectors(snapshot)
        restricted = [tools["user.delete"], tools["role.create"]]
        for query in (
            "创建用户",
            "user.self.update",
            "暂时让这个同事不能登录",
            "我想换个昵称",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    initial_operations(
                        [{"role": "user", "content": query}], restricted, vectors
                    ),
                    [],
                )
        record = self.app.state.run_store.record_workflow

        def change_on_selection(run, entry):
            result = record(run, entry)
            if entry.get("phase") == "selected":
                self.update_catalog_revision()
            return result

        with patch.object(
            self.app.state.run_store, "record_workflow", side_effect=change_on_selection
        ):
            run = self.chat(
                [{"name": "agent_users_user_self_read", "args": {}}],
                prompt="查看我的账号",
            )
        self.assertIn("agent_users_user_self_read", self.model.offered[0])
        self.assertEqual(
            self.model.results[-1][0]["error"]["code"], "TOOL_VERSION_MISMATCH"
        )
        self.assertEqual(self.model.offered[-1], [SEARCH_NAME])
        self.assertEqual(run["executions"], [])

    def test_hybrid_top_three_and_unserved_queries(self):
        snapshot = self.app.state.tool_registry.snapshot()
        tools = list(configurable_tools(snapshot).values())
        vectors = catalog_vectors(snapshot)
        for question, expected in INTENT_CASES:
            with self.subTest(question=question):
                ranked = initial_operations(
                    [{"role": "user", "content": question}], tools, vectors
                )
                self.assertIn(expected, ranked)
                self.assertLessEqual(len(ranked), CANDIDATE_LIMIT)
                if expected != "user.delete":
                    self.assertNotIn("user.delete", ranked)
        for question in (
            "天气预报",
            "查询订单",
            "明年我想换个城市",
            "请处理操作",
            "你有哪些工具？",
        ):
            self.assertEqual(
                initial_operations(
                    [{"role": "user", "content": question}], tools, vectors
                ),
                [],
            )

        for query, expected, wrong_subject in (
            ("我想换个昵称", "user.self.update", "user.update"),
            ("给同事换个昵称", "user.update", "user.self.update"),
            ("查同事的模型用量", "user.token_usage", "user.self.token_usage"),
        ):
            with self.subTest(query=query):
                ranked = matching_operations(tools, query, index=vectors)
                self.assertEqual(ranked[0], expected)
                self.assertNotIn(wrong_subject, ranked)

    def test_catalog_revision_refits_the_vectors_for_published_tools(self):
        original = self.app.state.tool_registry.snapshot()
        old_index = catalog_vectors(original)
        self.assertIs(catalog_vectors(original), old_index)
        future = {
            "binding": {
                "operationId": "billing.export",
                "policy": {"authentication": "session", "effect": "read"},
            },
            "definition": {"name": "billing_export", "description": "导出发票凭证。"},
        }
        changed = {
            **original,
            "bundleDigest": "next",
            "tools": [*original["tools"], future],
        }
        revised = catalog_vectors(changed)
        self.assertNotIn("billing.export", old_index.rows)
        self.assertEqual(
            matching_operations([future], "导出发票", index=revised), ["billing.export"]
        )
        self.assertEqual(
            matching_operations(original["tools"], "billing.export", index=revised), []
        )

    def test_directory_pages_show_only_metadata_and_never_bind_business_tools(self):
        expected = sorted(configurable_tools(self.app.state.tool_registry.snapshot()))
        last_page = ceil(len(expected) / PAGE_SIZE)
        page_numbers = list(range(1, last_page + 2))
        run = self.chat(*[[search(page=n)] for n in page_numbers])
        pages = [outputs[0] for outputs in self.model.results[1:]]
        self.assertEqual([p["page"] for p in pages], page_numbers)
        self.assertEqual(
            [p["next_page"] for p in pages],
            [n + 1 if n < last_page else None for n in page_numbers],
        )
        self.assertTrue(
            all(p["mode"] == "list" and p["total"] == len(expected) for p in pages)
        )
        self.assertTrue(all(len(p["tools"]) <= PAGE_SIZE for p in pages))
        items = [tool for page in pages for tool in page["tools"]]
        self.assertEqual([item["operation_id"] for item in items], expected)
        self.assertTrue(
            all(
                item.keys() == {"operation_id", "name", "title", "effect"}
                for item in items
            )
        )
        self.assertEqual(self.model.offered, [[SEARCH_NAME]] * (len(pages) + 1))
        self.assertIsNone(run["interaction"])
        self.assertEqual(run["executions"], [])

    def test_directory_and_search_use_the_callers_grants_and_role_permissions(self):
        member = {
            "username": "member",
            "display_name": "成员",
            "password": "test-password",
        }
        user = self.client.post(
            "/api/v1/users", json=member, headers=self.headers
        ).json()["data"]
        login = self.client.post(
            "/api/v1/auth/login",
            json={key: member[key] for key in ("username", "password")},
        )
        headers = {"Authorization": "Bearer " + login.json()["data"]["session_token"]}
        self.chat([search()], headers=headers)
        self.assertEqual(self.model.results[-1][0]["total"], 0)
        self.assertEqual(self.model.offered, [[SEARCH_NAME]] * 2)

        # role.create is delegated but the member role lacks it; user.self.update has RBAC only.
        grant_tools(
            self.client, self.headers, user["id"], ["user.self.read", "role.create"]
        )
        self.chat(
            [search()],
            [search("role.create user.self.update auth.login")],
            headers=headers,
        )
        self.assertEqual(self.model.results[1][0]["total"], 1)
        self.assertEqual(
            [item["operation_id"] for item in self.model.results[1][0]["tools"]],
            ["user.self.read"],
        )
        self.assertEqual(self.model.results[-1][0]["tools"], [])
        self.assertEqual(self.model.offered, [[SEARCH_NAME]] * 3)
        for prompt in ("我想换个昵称", "创建一个角色"):
            self.chat(headers=headers, prompt=prompt)
            self.assertEqual(self.model.offered, [[SEARCH_NAME]])
        self.chat(headers=headers, prompt="查看我的账号")
        self.assertEqual(
            self.model.offered,
            [[SEARCH_NAME, "agent_users_user_self_read", PREPARE_NAME]],
        )
        self.chat([search("role.create")])
        self.assertIn("agent_users_role_create", self.model.offered[-1])

    def test_rank_uses_registry_metadata_chinese_synonyms_and_exact_identifiers(self):
        tools = list(
            configurable_tools(self.app.state.tool_registry.snapshot()).values()
        )
        for query, expected in [
            ("查询用户列表", "user.list"),
            ("创建用户", "user.create"),
            ("为成员重设口令", "user.password.reset"),
            ("我的账号信息", "user.self.read"),
            ("查询我的 Token 消耗量", "user.self.token_usage"),
            ("查询指定用户的模型用量", "user.token_usage"),
            ("tokens", "user.self.token_usage"),
            ("list users", "user.list"),
            ("agent_users_role_options", "role.options"),
        ]:
            with self.subTest(query=query):
                ranked = matching_operations(tools, query)
                self.assertEqual(ranked[0], expected)
                self.assertLessEqual(len(ranked), CANDIDATE_LIMIT)
                self.assertEqual(ranked, matching_operations(tools[::-1], query))
        self.assertEqual(matching_operations(tools, "天气预报"), [])
        self.assertEqual(matching_operations(tools, "unavailable.create"), [])
        restricted = [
            t
            for t in tools
            if t["binding"]["operationId"]
            in {"user.delete", "role.create", "user.list"}
        ]
        for query in ("创建用户", "create user"):
            self.assertEqual(matching_operations(restricted, query), [])
        future = {
            "binding": {"operationId": "exportInvoices"},
            "definition": {"name": "billing_export", "description": "导出发票。"},
        }
        for query in ["导出发票", "billing_export", "exportInvoices"]:
            self.assertEqual(
                matching_operations([*tools, future], query), ["exportInvoices"]
            )

    def test_model_usage_tool_is_found_and_executes_with_date_parameters(self):
        name = "agent_users_user_self_token_usage"
        run = self.chat(
            [search("查询我的 Token 消耗量")],
            [{"name": name, "args": {"query": {"from_date": "2026-11-17"}}}],
        )
        self.assertEqual(self.model.offered[0], [SEARCH_NAME])
        self.assertIn(name, self.model.offered[1])
        self.assertIsNone(run["interaction"])
        self.assertEqual(run["executions"][0]["operation_id"], "user.self.token_usage")
        result = self.model.results[-1][0]["body"]["data"]
        self.assertEqual(result["user_id"], self.user_id)
        self.assertEqual(result["from_date"], "2026-11-17")

    def test_each_search_replaces_candidates_and_a_new_run_starts_empty(self):
        read = {"name": "agent_users_user_self_read", "args": {}}
        run = self.chat(
            [search("user.self.read")],
            [read],
            [search("role.create")],
            [search("天气预报")],
            [read],
        )
        read_tools = [SEARCH_NAME, read["name"], PREPARE_NAME]
        self.assertEqual(
            self.model.offered,
            [
                [SEARCH_NAME],
                read_tools,
                read_tools,
                [SEARCH_NAME, "agent_users_role_create", PREPARE_NAME],
                [SEARCH_NAME],
                [SEARCH_NAME],
            ],
        )
        self.assertEqual(self.model.results[2][0]["body"]["data"]["username"], "admin")
        self.assertEqual(self.model.results[-1][0]["error"]["code"], "TOOL_NOT_LOADED")
        self.assertEqual(
            [item["operation_id"] for item in run["executions"]], ["user.self.read"]
        )
        self.chat([search("role.create")], [search()])
        self.assertEqual(
            self.model.offered,
            [
                [SEARCH_NAME],
                [SEARCH_NAME, "agent_users_role_create", PREPARE_NAME],
                [SEARCH_NAME],
            ],
        )
        self.chat()
        self.assertEqual(self.model.offered, [[SEARCH_NAME]])

    def test_same_response_search_never_authorizes_a_sibling_call_or_prepare(self):
        arguments = {"body": {"code": "guessed_role", "display_name": "测试角色"}}
        calls = [
            search("role.create"),
            {"name": "agent_users_role_create", "args": arguments},
            {
                "name": PREPARE_NAME,
                "args": {"operation_id": "role.create", "arguments": arguments},
            },
        ]
        run = self.chat(calls)
        results = self.model.results[-1]
        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(
            [r["error"]["code"] for r in results[1:]], ["TOOL_NOT_LOADED"] * 2
        )
        self.assertEqual(
            self.model.offered[-1],
            [SEARCH_NAME, "agent_users_role_create", PREPARE_NAME],
        )
        self.assertIsNone(run["interaction"])
        self.assertEqual(run["executions"], [])
        self.assertEqual(
            self.client.get(
                "/api/v1/roles/guessed_role", headers=self.headers
            ).status_code,
            404,
        )

        disabled = self.chat(calls, tool_choice="none")
        self.assertEqual(self.model.offered, [[], []])
        self.assertEqual(
            [r["error"]["code"] for r in self.model.results[-1]],
            ["TOOLS_DISABLED"] * len(calls),
        )
        self.assertEqual(disabled["executions"], [])

    def test_revocation_or_new_registry_revision_invalidates_search_before_binding(
        self,
    ):
        store = self.app.state.run_store
        record = store.record_workflow
        for change in ("revoke", "new_revision"):
            with self.subTest(change=change):
                grant_tools(self.client, self.headers, self.user_id, ["user.list"])

                def after_search(run, entry, change=change):
                    result = record(run, entry)
                    if (
                        entry["id"].startswith("search:")
                        and entry["status"] == "succeeded"
                    ):
                        if change == "revoke":
                            grant_tools(
                                self.client,
                                self.headers,
                                self.user_id,
                                ["user.list"],
                                enabled=False,
                            )
                        else:
                            self.update_catalog_revision()
                    return result

                with patch.object(store, "record_workflow", side_effect=after_search):
                    run = self.chat(
                        [search("user.list")],
                        [{"name": "agent_users_user_list", "args": {}}],
                    )
                self.assertEqual(self.model.results[1][0]["count"], 1)
                self.assertEqual(self.model.offered, [[SEARCH_NAME]] * 3)
                self.assertEqual(
                    self.model.results[-1][0]["error"]["code"], "TOOL_NOT_LOADED"
                )
                self.assertEqual(run["executions"], [])
        self.chat([search("user.list")])
        self.assertIn("agent_users_user_list", self.model.offered[-1])

    def test_catalog_changed_after_model_selection_requires_a_fresh_search(self):
        store = self.app.state.run_store
        record = store.record_workflow
        read = {"name": "agent_users_user_self_read", "args": {}}

        def change_before_execution(run, entry):
            result = record(run, entry)
            if entry.get("phase") == "selected" and entry["id"].startswith("tool:2:"):
                self.update_catalog_revision()
            return result

        with patch.object(
            store, "record_workflow", side_effect=change_before_execution
        ):
            run = self.chat(
                [search("user.self.read")], [read], [search("user.self.read")], [read]
            )
        self.assertEqual(
            self.model.results[2][0]["error"]["code"], "TOOL_VERSION_MISMATCH"
        )
        self.assertEqual(self.model.offered[2], [SEARCH_NAME])
        self.assertEqual(self.model.results[4][0]["body"]["data"]["username"], "admin")
        self.assertEqual(
            [item["operation_id"] for item in run["executions"]], ["user.self.read"]
        )

    def test_query_secrets_and_invalid_arguments_stay_out_of_persisted_state(self):
        secret = "private-value-for-this-test"
        invalid = [
            {"name": SEARCH_NAME, "args": args}
            for args in (
                {},
                {"query": secret, "password": secret},
                {"query": "", "page": True},
                {"query": secret * 12},
                {"query": "user.create", "page": 2},
            )
        ]
        run = self.chat([search("重置用户密码 " + secret)], invalid)
        self.assertEqual(
            self.model.results[1][0]["tools"][0]["operation_id"], "user.password.reset"
        )
        self.assertTrue(
            all(
                item["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
                for item in self.model.results[-1]
            )
        )
        self.assertEqual(self.model.offered[-1], [SEARCH_NAME])
        checkpoints = list(
            DatabaseCheckpointer(self.app.state.database).list(
                {"configurable": {"thread_id": run["id"]}}
            )
        )
        self.assertTrue(checkpoints)
        self.assertNotIn(secret, repr(checkpoints))
        self.assertNotIn(secret, repr(run))
