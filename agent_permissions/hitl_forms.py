"""Trusted forms derived from the live tool contract, with application display hints."""

import copy
from typing import Any
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from agent_permissions.agent_grants import _label
from agent_permissions.errors import ApiError, invalid_field
from agent_permissions.models import (
    PasswordChange,
    PasswordReset,
    RoleCreate,
    RolesUpdate,
    RoleUpdate,
    StatusUpdate,
    UserCreate,
    UserUpdate,
)

LABELS = {
    "username": "用户名",
    "display_name": "展示名称",
    "email": "邮箱",
    "password": "初始密码",
    "new_password": "新密码",
    "current_password": "当前密码",
    "roles": "角色",
    "role": "角色",
    "status": "账号状态",
    "user_id": "目标用户",
    "role_code": "目标角色",
    "code": "角色编码",
    "description": "说明",
    "permissions": "权限",
    "page": "页码",
    "page_size": "每页数量",
    "q": "查询关键词",
    "sort_by": "排序字段",
    "sort_order": "排序方向",
}
SECRET_NAMES = {
    "password",
    "new_password",
    "current_password",
    "confirm_password",
    "password_hash",
    "session_token",
    "authorization",
    "api_key",
}
BODY_MODELS = {
    "user.create": UserCreate,
    "user.update": UserUpdate,
    "user.self.update": UserUpdate,
    "user.roles.assign": RolesUpdate,
    "user.status.update": StatusUpdate,
    "user.password.reset": PasswordReset,
    "user.self.password.change": PasswordChange,
    "role.create": RoleCreate,
    "role.update": RoleUpdate,
}


def scrub(value: Any) -> Any:
    """Never persist model-proposed secret arguments or return form secrets to the model."""
    if isinstance(value, dict):
        return {
            key: scrub(item)
            for key, item in value.items()
            if key.lower() not in SECRET_NAMES
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def safe_arguments(tool: dict, arguments: dict) -> dict:
    """Also honor schema-marked secrets whose property names are application-specific."""
    clean = scrub(arguments)
    for field in fields_for(tool["definition"]["inputSchema"], clean):
        if not field["sensitive"]:
            continue
        node = clean
        path = parts(field["pointer"])
        for part in path[:-1]:
            if not isinstance(node.get(part), dict):
                # A malformed container can itself hold an opaque secret.
                node.pop(part, None)
                break
            node = node[part]
        else:
            node.pop(path[-1], None)
    return clean


def parts(pointer: str) -> list[str]:
    return [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer.lstrip("/").split("/")
    ]


def read_pointer(value: dict, pointer: str) -> tuple[bool, Any]:
    current: Any = value
    for part in parts(pointer):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def write_pointer(value: dict, pointer: str, item: Any) -> None:
    path = parts(pointer)
    for part in path[:-1]:
        value = value.setdefault(part, {})
    value[path[-1]] = item


def simple_schema(schema: dict) -> dict:
    result = {
        key: value for key, value in schema.items() if key not in {"allOf", "anyOf"}
    }
    for branch in schema.get("allOf", []):
        result.update(simple_schema(branch))
    if "anyOf" in schema:
        branches = [item for item in schema["anyOf"] if item.get("type") != "null"]
        if len(branches) != 1:
            raise ApiError(
                422, "INTERACTION_UNSUPPORTED_SCHEMA", "此工具参数暂不支持表单填写"
            )
        result.update(simple_schema(branches[0]))
    return result


def fields_for(schema: dict, arguments: dict) -> list[dict]:
    fields = []

    def visit(node: dict, path: str, required: bool) -> None:
        simple = simple_schema(node)
        kind = simple.get("type")
        if kind == "object":
            if simple.get("additionalProperties") not in (False, None):
                raise ApiError(
                    422,
                    "INTERACTION_UNSUPPORTED_SCHEMA",
                    "动态对象参数暂不支持表单填写",
                )
            for name, child in simple.get("properties", {}).items():
                if path == "" and name == "header":
                    continue
                pointer = path + "/" + name.replace("~", "~0").replace("/", "~1")
                visit(child, pointer, name in simple.get("required", []))
            return
        if kind not in {"string", "integer", "number", "boolean", "array"} or (
            kind == "array" and simple.get("items", {}).get("type") != "string"
        ):
            raise ApiError(
                422, "INTERACTION_UNSUPPORTED_SCHEMA", "此工具参数暂不支持表单填写"
            )
        name = parts(path)[-1]
        secret = (
            simple.get("format") == "password"
            or simple.get("writeOnly") is True
            or name in SECRET_NAMES
        )
        present, value = read_pointer(arguments, path)
        field = {
            "pointer": path,
            "label": LABELS.get(name, simple.get("title", name)),
            "type": kind,
            "required": required,
            "nullable": Draft202012Validator(node).is_valid(None),
            "sensitive": secret,
            "editable": not (path.startswith("/path/") and present),
            "present": present and not secret,
            "source": None,
            "options": [
                {
                    "value": item,
                    "label": {
                        "active": "启用",
                        "disabled": "禁用",
                        "asc": "升序",
                        "desc": "降序",
                    }.get(item, str(item)),
                }
                for item in simple.get("enum", simple.get("items", {}).get("enum", []))
            ],
            "constraints": {
                key: simple[key]
                for key in (
                    "minLength",
                    "maxLength",
                    "minimum",
                    "maximum",
                    "minItems",
                    "maxItems",
                    "pattern",
                    "format",
                )
                if key in simple
            },
        }
        if present and not secret:
            field["value"] = value
            field["value_source"] = "proposal"
        if name in {"roles", "role", "role_code"}:
            field["source"] = "roles"
        elif name == "permissions":
            field["source"] = "permissions"
        elif name == "user_id":
            field["source"] = "users"
        fields.append(field)

    visit(schema, "", True)
    return fields


def validate_arguments(tool: dict, arguments: dict, *, partial: bool = False) -> None:
    errors = list(
        Draft202012Validator(
            tool["definition"]["inputSchema"], format_checker=FormatChecker()
        ).iter_errors(arguments)
    )
    if partial:
        errors = [error for error in errors if error.validator != "required"]
    if errors:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "请检查工具参数",
            details=[
                {
                    "field": "/" + "/".join(str(part) for part in error.absolute_path),
                    "message": "字段类型、取值或必填要求不符合接口规则",
                }
                for error in errors[:20]
            ],
        )
    model = BODY_MODELS.get(tool["binding"]["operationId"])
    if not partial and model:
        try:
            model.model_validate(arguments.get("body", {}))
        except ValidationError as error:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "请检查表单字段",
                details=[
                    {
                        "field": "/body/" + "/".join(str(part) for part in item["loc"]),
                        "message": "字段值不符合业务规则",
                    }
                    for item in error.errors(include_input=False)[:20]
                ],
            ) from None


def requires_confirmation(tool: dict) -> bool:
    policy = tool["binding"]["policy"]
    return (
        policy["effect"] != "read"
        or policy.get("interactionPolicy") == "always_confirm"
    )


async def get_data(
    http: httpx.AsyncClient, path: str, authorization: str, params: dict | None = None
) -> tuple[Any, str | None]:
    response = await http.get(
        path,
        headers={"Authorization": authorization},
        params=params,
        follow_redirects=False,
    )
    body = response.json()
    if not response.is_success:
        error = body.get("error", {})
        raise ApiError(
            response.status_code,
            error.get("code", "PREPARATION_FAILED"),
            error.get("message", "无法读取操作对象"),
        )
    return body["data"], response.headers.get("ETag")


def target_path(operation: str, arguments: dict) -> str | None:
    path = arguments.get("path", {})
    if operation.startswith("user.self."):
        return "/api/v1/users/me"
    if operation.startswith("user.") and path.get("user_id"):
        return "/api/v1/users/" + quote(path["user_id"], safe="")
    if operation.startswith("role.") and path.get("role_code"):
        return "/api/v1/roles/" + quote(path["role_code"], safe="")
    return None


async def prepare_form(
    tool: dict, arguments: dict, http: httpx.AsyncClient, authorization: str
) -> dict:
    draft = safe_arguments(tool, copy.deepcopy(arguments))
    draft.pop("header", None)
    validate_arguments(tool, draft, partial=True)
    operation = tool["binding"]["operationId"]
    confirm = requires_confirmation(tool)
    target = None
    path = target_path(operation, draft) if confirm else None
    if path:
        data, etag = await get_data(http, path, authorization)
        target = {
            key: data[key]
            for key in (
                "id",
                "code",
                "username",
                "display_name",
                "roles",
                "status",
                "version",
            )
            if key in data
        }
        if "header" in tool["definition"]["inputSchema"].get("properties", {}):
            if not etag:
                raise ApiError(
                    409, "RESOURCE_VERSION_CONFLICT", "操作对象缺少版本信息，请重新读取"
                )
            draft["header"] = {"If-Match": etag}
    fields = fields_for(tool["definition"]["inputSchema"], draft)
    missing = any(field["required"] and not field["present"] for field in fields)
    return {
        "title": _label(operation, tool)[0],
        "kind": "collect_parameters" if missing else "confirm_action",
        "confirmation_required": confirm,
        "effect": tool["binding"]["policy"]["effect"],
        "fields": fields,
        "arguments": draft,
        "target": target,
        "description": "请核对操作对象与参数。确认后执行，实际操作仍受当前账号权限限制。"
        if confirm
        else "请补充查询参数，提交后继续。",
    }


def apply_values(document: dict, values: dict, sensitive: dict) -> dict:
    arguments = copy.deepcopy(document["arguments"])
    fields = {item["pointer"]: item for item in document["fields"]}
    for supplied, secret in ((values, False), (sensitive, True)):
        for pointer, value in supplied.items():
            field = fields.get(pointer)
            if not field or not field["editable"] or field["sensitive"] != secret:
                raise invalid_field(pointer, "此字段不允许在此处修改")
            write_pointer(arguments, pointer, value)
    return arguments


async def check_target(
    document: dict,
    operation: str,
    arguments: dict,
    http: httpx.AsyncClient,
    authorization: str,
) -> None:
    expected = document["arguments"].get("header", {}).get("If-Match")
    if expected:
        path = target_path(operation, arguments)
        _, current = await get_data(http, path, authorization)
        if current != expected:
            raise ApiError(
                412, "RESOURCE_VERSION_CONFLICT", "操作对象已变化，请重新准备并确认"
            )


async def field_options(
    source: str, http: httpx.AsyncClient, authorization: str, query: str
) -> list[dict]:
    if source == "users":
        data, _ = await get_data(
            http,
            "/api/v1/users",
            authorization,
            {"q": query, "page_size": 50} if query else {"page_size": 50},
        )
        return [
            {
                "value": item["id"],
                "label": f"{item['display_name']}（{item['username']}）",
            }
            for item in data["items"]
        ]
    path = "/api/v1/role-options" if source == "roles" else "/api/v1/permissions"
    data, _ = await get_data(http, path, authorization)
    return [
        {"value": item["code"], "label": f"{item['display_name']}（{item['code']}）"}
        for item in data
        if not query
        or query.casefold() in (item["display_name"] + item["code"]).casefold()
    ]
