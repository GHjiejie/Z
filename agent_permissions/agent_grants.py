"""User delegation data and views; role changes never modify these records."""

from collections.abc import Mapping
from typing import Any

from agent_permissions.errors import ApiError, invalid_field
from agent_permissions.models import AgentToolGrantsUpdate
from agent_permissions.permissions import PERMISSION_CATALOG, permissions_for
from agent_permissions.storage import Connection
from api_tools.registry import permitted


def grants_for(connection: Connection, user_id: str) -> dict[str, bool]:
    return {
        row["operation_id"]: bool(row["enabled"])
        for row in connection.execute(
            "SELECT operation_id,enabled FROM user_agent_tool_grants WHERE user_id=?",
            (user_id,),
        )
    }


def configurable_tools(snapshot: dict) -> dict[str, dict]:
    return {
        tool["binding"]["operationId"]: tool
        for tool in snapshot["tools"]
        if tool["binding"]["policy"]["authentication"] == "session"
    }


def _label(operation: str, tool: dict) -> tuple[str, str]:
    catalog = {code: (name, group) for code, name, group in PERMISSION_CATALOG}
    names = {
        "auth.logout": ("退出当前登录", "个人账号"),
        "user.self.read": ("查看自己的资料", "个人账号"),
        "user.self.permissions": ("查看自己的权限", "个人账号"),
        "user.list": ("查询用户列表", "用户管理"),
        "user.read": ("查看用户详情", "用户管理"),
        "role.list": ("查询角色列表", "角色管理"),
        "role.read": ("查看角色详情", "角色管理"),
        "role.options": ("查询角色选项", "角色管理"),
        "permission.list": ("查看权限目录", "角色管理"),
    }
    return (
        names.get(operation)
        or catalog.get(operation)
        or (
            tool["definition"]["description"].split("。", 1)[0],
            "其他工具",
        )
    )


def config_view(
    connection: Connection,
    target: Mapping[str, Any],
    snapshot: dict,
    *,
    can_manage: bool,
) -> dict:
    saved = grants_for(connection, target["id"])
    permissions = set(permissions_for(connection, target["id"]))
    tools = configurable_tools(snapshot)
    items = []
    for operation in sorted(set(tools) | set(saved)):
        tool = tools.get(operation)
        enabled = saved.get(operation, False)
        policy = tool["binding"]["policy"] if tool else {}
        rbac = bool(tool) and permitted(policy, permissions, authenticated=True)
        if not tool:
            state = "unavailable"
        elif target["status"] != "active":
            state = "account_disabled"
        elif not enabled:
            state = "not_granted"
        elif not rbac:
            state = "missing_business_permission"
        else:
            state = "ready"
        name, resource = _label(operation, tool) if tool else (operation, "已下线工具")
        items.append(
            {
                "operation_id": operation,
                "tool_name": tool["definition"]["name"] if tool else None,
                "display_name": name,
                "resource": resource,
                "effect": policy.get("effect", "unknown"),
                "enabled": enabled,
                "rbac_allowed": rbac,
                "available": tool is not None,
                "state": state,
                "required_permissions": {
                    "all": policy.get("allPermissions", []),
                    "any": policy.get("anyPermissions", []),
                },
            }
        )
    return {
        "user_id": target["id"],
        "version": target["agent_tools_version"],
        "catalog_revision": snapshot["bundleDigest"],
        "can_manage": can_manage,
        "items": items,
    }


def save_changes(
    connection: Connection,
    target: Mapping[str, Any],
    payload: AgentToolGrantsUpdate,
    snapshot: dict,
    *,
    actor_id: str,
    now: str,
) -> list[dict]:
    if payload.expected_version != target["agent_tools_version"]:
        raise ApiError(412, "VERSION_CONFLICT", "工具授权已变化，请重新读取后操作")
    if payload.catalog_revision != snapshot["bundleDigest"]:
        raise ApiError(409, "TOOL_VERSION_MISMATCH", "工具目录已变化，请刷新后操作")
    saved, tools = grants_for(connection, target["id"]), configurable_tools(snapshot)
    changes = []
    # Validate the whole batch before writing, including no-op entries.
    for change in payload.changes:
        operation = change.operation_id
        if operation not in tools and (operation not in saved or change.enabled):
            raise invalid_field("changes", "工具不存在、不可授权或已下线")
        old = saved.get(operation, False)
        if old != change.enabled:
            changes.append(
                {"operation_id": operation, "before": old, "after": change.enabled}
            )
    for change in changes:
        connection.execute(
            """INSERT INTO user_agent_tool_grants(user_id,operation_id,enabled,updated_by,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(user_id,operation_id) DO UPDATE SET
            enabled=excluded.enabled,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (target["id"], change["operation_id"], int(change["after"]), actor_id, now),
        )
    if changes:
        connection.execute(
            "UPDATE users SET agent_tools_version=agent_tools_version+1 WHERE id=?",
            (target["id"],),
        )
    return changes
