"""FastMCP request mappings for the versioned, grouped tool contract."""

import logging
from contextvars import ContextVar

import httpx2
from fastmcp.utilities.openapi import parse_openapi_to_http_routes
from fastmcp.utilities.openapi.director import RequestDirector
from jsonschema_path import SchemaPath

# The upstream director logs raw arguments at DEBUG. Keep credentials and business
# payloads out of logs for these calls, including when the host enables debug logs.
_building_request = ContextVar("agent_tools_building_request", default=False)
logging.getLogger("fastmcp.utilities.openapi.director").addFilter(
    lambda record: not _building_request.get() or record.levelno > logging.DEBUG
)


class OpenAPIRequests:
    """The same parsed FastMCP routes serve compilation and request execution."""

    def __init__(self, paths: dict) -> None:
        spec = {
            "openapi": "3.1.0",
            "info": {"title": "Agent tools", "version": "1"},
            "paths": paths,
        }
        self._director = RequestDirector(SchemaPath.from_dict(spec))
        self.routes = tuple(parse_openapi_to_http_routes(spec)) if paths else ()
        self._operations = {route.operation_id: route for route in self.routes}

    @classmethod
    def from_manifest(cls, bundle: dict) -> "OpenAPIRequests":
        """Standalone registries can also execute a verified, serialized v1 manifest."""
        paths = {}
        for tool in bundle["tools"]:
            binding = tool["binding"]
            schema = tool["definition"]["inputSchema"]
            groups = schema["properties"]
            parameters = []
            for param in binding["parameters"]:
                location, name = param["in"], param["name"]
                group = groups[location]
                parameters.append(
                    {
                        "in": location,
                        "name": name,
                        "explode": param["explode"],
                        "schema": group["properties"][name],
                        "required": name in group["required"],
                    }
                )
            operation = {
                "operationId": binding["operationId"],
                "parameters": parameters,
                "responses": {"200": {"description": "Business response"}},
            }
            if "body" in groups:
                operation["requestBody"] = whole_json_body(
                    groups["body"], required="body" in schema["required"]
                )
            paths.setdefault(binding["path"], {})[binding["method"].lower()] = operation
        return cls(paths)

    def build(self, operation_id: str, arguments: dict) -> httpx2.Request:
        route = self._operations[operation_id]
        flat = {}
        for alias, parameter in route.parameter_map.items():
            location, name = parameter["location"], parameter["openapi_name"]
            if location == "body":
                if "body" in arguments:
                    flat[alias] = arguments["body"]
            elif name in arguments.get(location, {}):
                value = arguments[location][name]
                if location == "path" and "/" in str(value):
                    raise ValueError("A path parameter must occupy one segment")
                if (
                    location == "query"
                    and value == []
                    and any(
                        p.location == "query" and p.name == name and p.explode is False
                        for p in route.parameters
                    )
                ):
                    # A non-exploded, explicitly empty list is one empty query value.
                    value = ""
                flat[alias] = value
        token = _building_request.set(True)
        try:
            request = self._director.build(route, flat)
        finally:
            _building_request.reset(token)
        # The director represents an entire JSON null and an absent body as None.
        if "body" in arguments and arguments["body"] is None:
            headers = dict(request.headers)
            headers.pop("content-length", None)
            headers["content-type"] = "application/json"
            return httpx2.Request(
                request.method, request.url, headers=headers, content=b"null"
            )
        return request


def whole_json_body(schema: dict, *, required: bool) -> dict:
    """A single-branch union keeps the entire JSON value as one FastMCP argument."""
    return {
        "required": required,
        "content": {"application/json": {"schema": {"anyOf": [schema]}}},
    }
