"""Compile actual FastAPI routes and OpenAPI into a deterministic tool bundle."""

import copy
import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator
from starlette.routing import BaseRoute

GENERATOR_VERSION = "1"
TARGET_PROFILE = "json-schema-2020-12"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
RESERVED_HEADERS = {"authorization", "cookie", "host", "content-type", "content-length"}


class ContractError(ValueError):
    """A contract cannot be represented without losing its semantics."""


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def route_inventory(routes: list[BaseRoute]) -> Iterator[Any]:
    """Support both flattened FastAPI routers and newer lazy include contexts."""
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            yield from contexts()
        else:
            yield route


def _resolve(value: Any, document: dict, stack: tuple[str, ...] = ()) -> Any:
    if isinstance(value, list):
        return [_resolve(item, document, stack) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not reference.startswith("#/") or reference in stack:
            raise ContractError(
                f"Unsupported external or recursive reference: {reference}"
            )
        target = document
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise ContractError(f"Unresolved reference: {reference}") from None
        resolved = _resolve(target, document, (*stack, reference))
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        # JSON Schema reference siblings are conjunctive, not a dictionary override.
        return (
            {"allOf": [resolved, _resolve(siblings, document, stack)]}
            if siblings
            else resolved
        )
    if "$dynamicRef" in value or "nullable" in value:
        raise ContractError("Unsupported schema dialect or dynamic reference")
    return {key: _resolve(item, document, stack) for key, item in value.items()}


def _json_schema(content: dict, *, empty: bool = False) -> dict:
    if not content:
        if empty:
            return {"type": "null"}
        raise ContractError("JSON body needs an explicit schema")
    if (
        set(content) != {"application/json"}
        or "schema" not in content["application/json"]
    ):
        raise ContractError(
            "Only application/json with an explicit schema is supported"
        )
    return content["application/json"]["schema"]


def _object(properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _input_schema(operation: dict, parameters: list) -> tuple[dict, list]:
    groups: dict[str, dict] = {}
    bindings = []
    required_groups = []
    for parameter in parameters:
        location, name = parameter["in"], parameter["name"]
        if location not in {"path", "query", "header"}:
            raise ContractError(f"Unsupported parameter location: {location}.{name}")
        if location == "header" and (
            name.lower() in RESERVED_HEADERS or name.lower().startswith("x-agent-tool")
        ):
            raise ContractError(
                f"Credential or transport header must come from context: {name}"
            )
        if (
            "schema" not in parameter
            or parameter.get("allowReserved")
            or parameter.get("allowEmptyValue")
        ):
            raise ContractError(f"Unsupported serialization: {location}.{name}")
        schema = parameter["schema"]
        types = _types(schema) - {"null"}
        style = parameter.get("style", "form" if location == "query" else "simple")
        explode = parameter.get("explode", location == "query")
        array = types == {"array"}
        if types - {"string", "integer", "number", "boolean", "array"}:
            raise ContractError(f"Object parameters need a dedicated adapter: {name}")
        if array and (
            _types(schema.get("items", {})) - {"string", "integer", "number", "boolean"}
        ):
            raise ContractError(f"Only scalar array items are supported: {name}")
        if style != ("form" if location == "query" else "simple") or (
            location != "query" and explode
        ):
            raise ContractError(f"Unsupported style/explode: {location}.{name}")
        if location != "query" and "null" in _types(schema):
            # Optional nullable headers in FastAPI represent absence. Explicit null is rejected.
            schema = {"allOf": [schema, {"not": {"type": "null"}}]}
        if location == "query" and "null" in _types(schema):
            # HTTP has no lossless null query encoding; omission and null stay distinct.
            schema = {"allOf": [schema, {"not": {"type": "null"}}]}
        group = groups.setdefault(location, _object())
        if name in group["properties"]:
            raise ContractError(f"Duplicate parameter: {location}.{name}")
        group["properties"][name] = schema
        if parameter.get("required"):
            group["required"].append(name)
            if location not in required_groups:
                required_groups.append(location)
        bindings.append(
            {"in": location, "name": name, "array": array, "explode": explode}
        )
    body = operation.get("requestBody")
    if body:
        groups["body"] = _json_schema(body.get("content", {}))
        if body.get("required"):
            required_groups.append("body")
    return _object(groups, required_groups), bindings


def _types(schema: dict) -> set[str]:
    if "type" in schema:
        return set(
            schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        )
    if "anyOf" in schema:
        return set().union(*(_types(item) for item in schema["anyOf"]))
    raise ContractError("Parameters require an explicit scalar or array type")


def _compile_operation(
    namespace: str, path: str, method: str, operation: dict, inherited: list
) -> dict:
    operation_id = operation.get("operationId")
    policy = operation.get("x-agent", {})
    description = operation.get("description") or operation.get("summary")
    if not operation_id or not description or policy.get("expose") is not True:
        raise ContractError(
            f"Missing explicit identity, description or exposure: {method} {path}"
        )
    for key in (
        "authentication",
        "allPermissions",
        "anyPermissions",
        "effect",
        "idempotent",
    ):
        if key not in policy:
            raise ContractError(f"Missing x-agent.{key}: {operation_id}")
    if policy["authentication"] not in {"none", "session"} or policy["effect"] not in {
        "read",
        "write",
        "destructive",
    }:
        raise ContractError(f"Invalid access or effect metadata: {operation_id}")
    if not isinstance(policy["idempotent"], bool) or any(
        not isinstance(policy[key], list)
        or any(not isinstance(p, str) for p in policy[key])
        for key in ("allPermissions", "anyPermissions")
    ):
        raise ContractError(
            f"Invalid permission or idempotency metadata: {operation_id}"
        )
    parameters = {(item["in"], item["name"]): item for item in inherited}
    parameters.update(
        {(item["in"], item["name"]): item for item in operation.get("parameters", [])}
    )
    input_schema, bindings = _input_schema(operation, list(parameters.values()))
    placeholders = set(re.findall(r"\{([^{}]+)\}", path))
    if placeholders != {p["name"] for p in bindings if p["in"] == "path"}:
        raise ContractError(f"Path parameters do not match template: {operation_id}")
    responses = {}
    for status, response in operation.get("responses", {}).items():
        if not re.fullmatch(r"[1-5][0-9]{2}", status):
            raise ContractError(
                f"Explicit response status required: {operation_id}.{status}"
            )
        responses[status] = _json_schema(response.get("content", {}), empty=True)
    if not any(status.startswith("2") for status in responses):
        raise ContractError(f"Missing success response: {operation_id}")
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{namespace}_{operation_id}")
    if len(name) > 64:
        raise ContractError(f"Tool name exceeds 64 characters: {name}")
    body_schemas = list({canonical(item): item for item in responses.values()}.values())
    output_schema = _object(
        {
            "ok": {"type": "boolean"},
            "httpStatus": {"type": "integer"},
            "body": {"anyOf": body_schemas},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "requestId": {"type": "string"},
            "bundleDigest": {"type": "string"},
        },
        ["ok", "httpStatus", "body", "headers", "requestId", "bundleDigest"],
    )
    Draft202012Validator.check_schema(input_schema)
    Draft202012Validator.check_schema(output_schema)
    return {
        "definition": {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "outputSchema": output_schema,
            "annotations": {
                "readOnlyHint": policy["effect"] == "read",
                "destructiveHint": policy["effect"] == "destructive",
                "idempotentHint": policy["idempotent"],
            },
        },
        "binding": {
            "operationId": operation_id,
            "method": method.upper(),
            "path": path,
            "parameters": bindings,
            "responses": responses,
            "policy": policy,
        },
    }


def compile_app(
    app: FastAPI, *, namespace: str, exclusions: dict[str, str] | None = None
) -> dict:
    """Compile without opening a database, using a fresh schema rather than its cache."""
    exclusions = exclusions or {}
    document = get_openapi(
        title=app.title, version=app.version, routes=app.routes, openapi_version="3.1.0"
    )
    inventory = []
    for route in route_inventory(app.routes):
        original = getattr(route, "original_route", route)
        path = getattr(route, "path_format", getattr(route, "path", None))
        reason = exclusions.get(path)
        extra = getattr(route, "openapi_extra", None) or {}
        policy = extra.get("x-agent", {})
        if policy.get("expose") is False:
            reason = policy.get("reason")
            if not reason:
                raise ContractError(f"Excluded route needs a reason: {path}")
        if reason:
            inventory.append(
                {
                    "path": path,
                    "methods": sorted(getattr(route, "methods", None) or []),
                    "excluded": reason,
                }
            )
            continue
        if not isinstance(original, APIRoute) or not getattr(
            route, "include_in_schema", False
        ):
            raise ContractError(f"Unclassified or hidden route: {path}")
        for method in sorted(route.methods):
            if not route.operation_id:
                raise ContractError(f"Explicit operationId required: {method} {path}")
            inventory.append(
                {
                    "path": path,
                    "method": method.lower(),
                    "operationId": route.operation_id,
                }
            )
    tools = []
    seen = set()
    expected = set()
    for item in inventory:
        if "excluded" in item:
            continue
        key = (item["path"], item["method"])
        if key in expected:
            raise ContractError(f"Duplicate route: {key}")
        expected.add(key)
        operation = document.get("paths", {}).get(key[0], {}).get(key[1])
        if not operation or operation.get("operationId") != item["operationId"]:
            raise ContractError(f"Actual route missing from contract: {key}")
        inherited = document["paths"][key[0]].get("parameters", [])
        tool = _compile_operation(
            namespace,
            key[0],
            key[1],
            _resolve(operation, document),
            _resolve(inherited, document),
        )
        for identifier in (
            "operation:" + item["operationId"],
            "tool:" + tool["definition"]["name"],
        ):
            if identifier in seen:
                raise ContractError(f"Duplicate identity: {identifier}")
            seen.add(identifier)
        tools.append(tool)
    advertised = {
        (path, method)
        for path, operations in document.get("paths", {}).items()
        for method, operation in operations.items()
        if method in HTTP_METHODS
        and operation.get("x-agent", {}).get("expose") is not False
        and path not in exclusions
    }
    if advertised != expected:
        raise ContractError("Actual routes and OpenAPI operations differ")
    tools.sort(key=lambda tool: tool["definition"]["name"])
    bundle = {
        "generatorVersion": GENERATOR_VERSION,
        "targetProfile": TARGET_PROFILE,
        "namespace": namespace,
        "sourceRevision": digest({"contract": document, "inventory": inventory}),
        "tools": tools,
        "exclusions": [item for item in inventory if "excluded" in item],
    }
    return {**bundle, "bundleDigest": digest(bundle)}


def verify_bundle(bundle: dict) -> None:
    unsigned = {key: value for key, value in bundle.items() if key != "bundleDigest"}
    if bundle.get("bundleDigest") != digest(unsigned):
        raise ContractError("Tool bundle digest mismatch")


def compare_bundles(previous: dict, current: dict) -> dict:
    """Conservatively flag structural changes; optional top-level properties are additive."""
    old = {tool["definition"]["name"]: tool for tool in previous["tools"]}
    new = {tool["definition"]["name"]: tool for tool in current["tools"]}
    report: dict[str, list] = {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": [],
        "breaking": [],
    }
    report["breaking"].extend(report["removed"])
    for name in sorted(old.keys() & new.keys()):
        if old[name] == new[name]:
            continue
        report["changed"].append(name)
        before, after = copy.deepcopy(old[name]), copy.deepcopy(new[name])
        for schema in (before["definition"], after["definition"]):
            schema.pop("description", None)
        # A method/path change is safe only if the rest of the declared contract agrees.
        for tool in (before, after):
            tool["binding"].pop("method", None)
            tool["binding"].pop("path", None)
        if _additive_input(
            before["definition"]["inputSchema"], after["definition"]["inputSchema"]
        ):
            before["definition"].pop("inputSchema")
            after["definition"].pop("inputSchema")
            old_parameters = before["binding"].pop("parameters")
            new_parameters = after["binding"].pop("parameters")
            if any(parameter not in new_parameters for parameter in old_parameters):
                report["breaking"].append(name)
                continue
        if before != after:
            report["breaking"].append(name)
    return report


def _additive_input(previous: dict, current: dict) -> bool:
    if previous == current:
        return True
    if previous.get("type") != "object" or current.get("type") != "object":
        return False
    old, new = copy.deepcopy(previous), copy.deepcopy(current)
    old_props, new_props = old.pop("properties", {}), new.pop("properties", {})
    if old != new or not old_props.keys() <= new_props.keys():
        return False
    return all(
        _additive_input(schema, new_props[name]) for name, schema in old_props.items()
    )
