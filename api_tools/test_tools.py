"""Offline tests for compilation, version changes and HTTP execution semantics."""

import copy
import json
import logging
import unittest
from typing import Annotated
from unittest.mock import AsyncMock

import httpx
from fastapi import APIRouter, FastAPI, Header, Query
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from api_tools.compiler import (
    ContractError,
    compare_bundles,
    compile_app,
    compile_catalog,
    digest,
)
from api_tools.executor import ExecutionContext, ToolError, ToolExecutor
from api_tools.registry import ToolRegistry

DOCS = {
    "/openapi.json": "schema",
    "/docs": "docs",
    "/docs/oauth2-redirect": "callback",
    "/redoc": "docs",
}
POLICY = {
    "x-agent": {
        "expose": True,
        "authentication": "session",
        "allPermissions": ["item.read"],
        "anyPermissions": [],
        "effect": "read",
        "idempotent": True,
    }
}


def app_for(*, with_query: bool = False, required_query: bool = False) -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/items")

    def get_item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    def query_item(item_id: str, q: str = "all") -> dict[str, str]:
        return {"id": item_id, "query": q}

    def required_item(item_id: str, q: str) -> dict[str, str]:
        return {"id": item_id, "query": q}

    router.add_api_route(
        "/{item_id}",
        required_item if required_query else query_item if with_query else get_item,
        methods=["GET"],
        operation_id="item.read",
        description="Read an item",
        openapi_extra=POLICY,
    )
    app.include_router(router, prefix="/v1")
    return app


def compile_test(app: FastAPI) -> dict:
    return compile_app(app, namespace="shop", exclusions=DOCS)


class CompilerTests(unittest.TestCase):
    def test_deterministic_and_immune_to_stale_openapi_cache(self) -> None:
        app = app_for()
        before = compile_test(app)
        app.openapi()
        app.add_api_route(
            "/new",
            lambda: None,
            operation_id="item.new",
            description="New item",
            openapi_extra=POLICY,
        )
        after = compile_test(app)
        self.assertEqual(len(after["tools"]), 2)
        self.assertNotEqual(before["bundleDigest"], after["bundleDigest"])
        self.assertEqual(after, compile_test(app))
        self.assertEqual(after["tools"][1]["binding"]["path"], "/v1/items/{item_id}")

    def test_nested_router_prefix_is_preserved(self) -> None:
        child, parent, app = APIRouter(), APIRouter(), FastAPI()
        child.add_api_route(
            "/value",
            lambda: None,
            operation_id="nested",
            description="Nested",
            openapi_extra=POLICY,
        )
        parent.include_router(child, prefix="/inner")
        app.include_router(parent, prefix="/outer")
        self.assertEqual(
            compile_test(app)["tools"][0]["binding"]["path"], "/outer/inner/value"
        )

    def test_hidden_and_unclassified_routes_cannot_disappear(self) -> None:
        for hidden in (True, False):
            app = app_for()
            app.add_api_route("/forgotten", lambda: None, include_in_schema=not hidden)
            with self.subTest(hidden=hidden), self.assertRaises(ContractError):
                compile_test(app)

    def test_explicit_exclusion_needs_a_reason(self) -> None:
        app = app_for()
        app.add_api_route(
            "/health",
            lambda: None,
            include_in_schema=False,
            openapi_extra={"x-agent": {"expose": False}},
        )
        with self.assertRaises(ContractError):
            compile_test(app)

    def test_excluded_recursive_schema_does_not_break_supported_operations(
        self,
    ) -> None:
        class Node(BaseModel):
            children: list["Node"] = []

        app = app_for()
        app.add_api_route(
            "/unsupported",
            lambda: None,
            response_model=Node,
            openapi_extra={
                "x-agent": {"expose": False, "reason": "Recursive adapter pending"}
            },
        )
        self.assertEqual(len(compile_test(app)["tools"]), 1)

    def test_duplicate_routes_and_converted_name_collisions_are_rejected(self) -> None:
        for path, operation in [
            ("/v1/items/{item_id}", "other"),
            ("/different", "item_read"),
        ]:
            app = app_for()
            app.add_api_route(
                path,
                lambda: None,
                operation_id=operation,
                description="Other",
                openapi_extra=POLICY,
            )
            with self.subTest(path=path), self.assertRaises(ContractError):
                compile_test(app)

    def test_external_references_cannot_trigger_network_fetches(self) -> None:
        app = app_for()
        app.add_api_route(
            "/external",
            lambda: None,
            operation_id="external",
            description="External",
            openapi_extra={
                **POLICY,
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "https://invalid.example/schema"}
                        }
                    }
                },
            },
        )
        with self.assertRaisesRegex(ContractError, "external"):
            compile_test(app)

    def test_credential_headers_and_object_query_are_rejected(self) -> None:
        for parameter in [
            {"in": "header", "name": "Authorization", "schema": {"type": "string"}},
            {"in": "query", "name": "filter", "schema": {"type": "object"}},
        ]:
            app = app_for()
            app.add_api_route(
                "/unsafe",
                lambda: None,
                operation_id="unsafe",
                description="Unsafe",
                openapi_extra={**POLICY, "parameters": [parameter]},
            )
            with (
                self.subTest(parameter=parameter["name"]),
                self.assertRaises(ContractError),
            ):
                compile_test(app)

    def test_schema_and_mapping_change_together(self) -> None:
        old, new = compile_test(app_for()), compile_test(app_for(with_query=True))
        report = compare_bundles(old, new)
        self.assertEqual(report["changed"], ["shop_item_read"])
        self.assertEqual(report["breaking"], [])
        tool = new["tools"][0]
        self.assertIn(
            "q", tool["definition"]["inputSchema"]["properties"]["query"]["properties"]
        )
        self.assertIn("q", [p["name"] for p in tool["binding"]["parameters"]])
        required = compile_test(app_for(required_query=True))
        self.assertEqual(compare_bundles(old, required)["breaking"], ["shop_item_read"])

    def test_policy_and_response_changes_are_not_silently_compatible(self) -> None:
        old = compile_test(app_for())
        for mutation in ("policy", "response"):
            new = copy.deepcopy(old)
            if mutation == "policy":
                new["tools"][0]["binding"]["policy"]["allPermissions"] = ["admin"]
            else:
                new["tools"][0]["binding"]["responses"]["200"] = {"type": "null"}
            self.assertEqual(compare_bundles(old, new)["breaking"], ["shop_item_read"])

    def test_snapshot_copy_and_invalid_activation_leave_current_unchanged(self) -> None:
        bundle = compile_test(app_for())
        registry = ToolRegistry(bundle)
        detached = registry.snapshot()
        detached["tools"].clear()
        self.assertEqual(registry.snapshot(), bundle)
        tampered = copy.deepcopy(bundle)
        tampered["tools"][0]["binding"]["path"] = "/other"
        with self.assertRaises(ContractError):
            registry.activate(tampered, expected=bundle)
        unsigned = {
            key: value for key, value in tampered.items() if key != "bundleDigest"
        }
        tampered["bundleDigest"] = digest(unsigned)
        with self.assertRaises(ContractError):
            registry.activate(tampered, expected=bundle)
        self.assertEqual(registry.snapshot(), bundle)


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_arguments_survive_fastmcp_aliases_and_manifest_reload(
        self,
    ) -> None:
        app = FastAPI()

        def update() -> dict:
            return {}

        app.add_api_route(
            "/rows/{id}",
            update,
            methods=["POST"],
            operation_id="row.update",
            description="Update row",
            openapi_extra={
                **POLICY,
                "parameters": [
                    {
                        "in": "path",
                        "name": "id",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {"in": "query", "name": "id", "schema": {"type": "string"}},
                    {"in": "query", "name": "body", "schema": {"type": "string"}},
                    {
                        "in": "query",
                        "name": "tags",
                        "explode": False,
                        "schema": {"type": "array", "items": {"type": "string"}},
                    },
                ],
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"type": ["object", "null"]}}
                    }
                },
            },
        )
        bundle, requests = compile_catalog(app, namespace="shop", exclusions=DOCS)
        observed = []

        def handle(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://test",
            params={"workspace": "demo"},
        ) as client:
            for registry in (
                ToolRegistry(bundle, requests=requests),
                ToolRegistry(json.loads(json.dumps(bundle))),
            ):
                executor = ToolExecutor(registry)
                arguments = {
                    "path": {"id": "A B"},
                    "query": {"id": "Q", "body": "query", "tags": []},
                }
                for body in (None, {}, {"id": "body-id", "extra": [None, False]}):
                    result = await executor.call(
                        "shop_row_update",
                        {**arguments, "body": body},
                        revision=bundle["bundleDigest"],
                        context=ExecutionContext(frozenset({"item.read"}), True),
                        client=client,
                    )
                    request = observed[-1]
                    self.assertTrue(result["ok"])
                    self.assertEqual(
                        request.url.raw_path.split(b"?")[0], b"/rows/A%20B"
                    )
                    self.assertEqual(
                        dict(request.url.params),
                        {"id": "Q", "body": "query", "tags": "", "workspace": "demo"},
                    )
                    self.assertEqual(
                        request.headers["content-type"], "application/json"
                    )
                    self.assertEqual(json.loads(request.content), body)
                await executor.call(
                    "shop_row_update",
                    arguments,
                    revision=bundle["bundleDigest"],
                    context=ExecutionContext(frozenset({"item.read"}), True),
                    client=client,
                )
                self.assertEqual(observed[-1].content, b"")
                self.assertNotIn("content-type", observed[-1].headers)

    async def test_fastmcp_debug_logging_omits_business_arguments(self) -> None:
        bundle = compile_test(app_for(with_query=True))
        marker = "private-argument-for-log-check"
        upstream = logging.getLogger("fastmcp.utilities.openapi.director")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": "a"})
            ),
            base_url="http://test",
        ) as client:
            with self.assertLogs(upstream, level="DEBUG") as logs:
                upstream.debug("outside the agent request")
                await ToolExecutor(ToolRegistry(bundle)).call(
                    "shop_item_read",
                    {"path": {"item_id": "a"}, "query": {"q": marker}},
                    revision=bundle["bundleDigest"],
                    context=ExecutionContext(frozenset({"item.read"}), True),
                    client=client,
                )
        self.assertIn("outside the agent request", " ".join(logs.output))
        self.assertNotIn(marker, " ".join(logs.output))

    async def test_serialization_preserves_headers_arrays_false_null_and_omission(
        self,
    ) -> None:
        app = FastAPI()

        @app.patch(
            "/items/{item_id}",
            operation_id="item.update",
            description="Update",
            openapi_extra=POLICY,
        )
        def update(
            item_id: str,
            body: Payload,
            tags: Annotated[list[str], Query()],
            enabled: bool = False,
            version: Annotated[str | None, Header(alias="If-Match")] = None,
        ) -> dict:
            return {}

        bundle = compile_test(app)
        observed = []

        async def handle(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={"saved": True},
                headers={
                    "ETag": '"v2"',
                    "X-Request-ID": "inner-id",
                    "Set-Cookie": "secret",
                },
            )

        context = ExecutionContext(
            frozenset({"item.read"}), True, "Bearer host-token", "outer-id"
        )
        arguments = {
            "path": {"item_id": "abc"},
            "query": {"tags": ["a b", "中"], "enabled": False},
            "header": {"If-Match": '"v1"'},
            "body": {"name": None},
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://test"
        ) as client:
            executor = ToolExecutor(ToolRegistry(bundle))
            result = await executor.call(
                "shop_item_update",
                arguments,
                revision=bundle["bundleDigest"],
                context=context,
                client=client,
            )
            self.assertEqual(observed[0].url.params.get_list("tags"), ["a b", "中"])
            self.assertEqual(observed[0].url.params["enabled"], "false")
            self.assertEqual(observed[0].headers["If-Match"], '"v1"')
            self.assertEqual(observed[0].headers["Authorization"], "Bearer host-token")
            self.assertEqual(observed[0].content, b'{"name":null}')
            self.assertEqual(result["headers"], {"etag": '"v2"'})
            self.assertEqual(result["requestId"], "inner-id")
            Draft202012Validator(
                bundle["tools"][0]["definition"]["outputSchema"]
            ).validate(result)
            arguments["body"] = {}
            await executor.call(
                "shop_item_update",
                arguments,
                revision=bundle["bundleDigest"],
                context=context,
                client=client,
            )
            self.assertEqual(observed[1].content, b"{}")

    async def test_removed_tools_and_stale_versions_do_not_reach_http(self) -> None:
        old = compile_test(app_for())
        registry = ToolRegistry(old)
        removed = compile_test(FastAPI())
        self.assertEqual(compare_bundles(old, removed)["removed"], ["shop_item_read"])
        registry.activate(removed, expected=removed)
        client = AsyncMock(spec=httpx.AsyncClient)
        with self.assertRaises(ToolError) as caught:
            await ToolExecutor(registry).call(
                "shop_item_read",
                {"path": {"item_id": "a"}},
                revision=old["bundleDigest"],
                context=ExecutionContext(),
                client=client,
            )
        self.assertEqual(caught.exception.code, "TOOL_UNAVAILABLE")
        registry.activate(old, expected=old)
        with self.assertRaises(ToolError) as caught:
            await ToolExecutor(registry).call(
                "shop_item_read",
                {},
                revision=removed["bundleDigest"],
                context=ExecutionContext(),
                client=client,
            )
        self.assertEqual(caught.exception.code, "TOOL_VERSION_MISMATCH")
        client.request.assert_not_called()

    async def test_input_and_transport_errors_are_safe_and_never_retried(self) -> None:
        bundle = compile_test(app_for())
        client = AsyncMock(spec=httpx.AsyncClient)
        executor = ToolExecutor(ToolRegistry(bundle))
        context = ExecutionContext(frozenset({"item.read"}), True)
        with self.assertRaises(ToolError) as caught:
            await executor.call(
                "shop_item_read",
                {"password": "do-not-print"},
                revision=bundle["bundleDigest"],
                context=context,
                client=client,
            )
        self.assertEqual(caught.exception.code, "TOOL_INPUT_INVALID")
        self.assertNotIn("do-not-print", str(caught.exception))
        client.request.assert_not_called()
        client.request.side_effect = httpx.ReadTimeout("do-not-print")
        with self.assertRaises(ToolError) as caught:
            await executor.call(
                "shop_item_read",
                {"path": {"item_id": "a"}},
                revision=bundle["bundleDigest"],
                context=context,
                client=client,
            )
        self.assertEqual(caught.exception.code, "TOOL_RESULT_UNKNOWN")
        self.assertNotIn("do-not-print", str(caught.exception))
        self.assertEqual(client.request.await_count, 1)

    async def test_wrong_output_is_rejected_without_leaking_response(self) -> None:
        bundle = compile_test(app_for())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json="private-response")
            ),
            base_url="http://test",
        ) as client:
            with self.assertRaises(ToolError) as caught:
                await ToolExecutor(ToolRegistry(bundle)).call(
                    "shop_item_read",
                    {"path": {"item_id": "a"}},
                    revision=bundle["bundleDigest"],
                    context=ExecutionContext(frozenset({"item.read"}), True),
                    client=client,
                )
        self.assertEqual(caught.exception.code, "TOOL_OUTPUT_INVALID")
        self.assertNotIn("private-response", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
