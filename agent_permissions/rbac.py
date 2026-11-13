"""Role records and validated many-to-many relationships in one transaction."""

from agent_permissions.errors import ApiError, invalid_field
from agent_permissions.storage import Connection


def require_roles(connection: Connection, codes: list[str]) -> None:
    known = {row[0] for row in connection.execute("SELECT code FROM roles")}
    if not codes or not set(codes) <= known:
        raise invalid_field("roles", "请选择至少一个已存在的角色")


def require_permissions(connection: Connection, codes: list[str]) -> None:
    known = {row[0] for row in connection.execute("SELECT code FROM permissions")}
    if not set(codes) <= known:
        raise invalid_field("permissions", "存在未定义的操作权限")


def role_permissions(connection: Connection, code: str) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT permission_code FROM role_permissions WHERE role_code=? ORDER BY permission_code",
            (code,),
        )
    ]


def role_record(connection: Connection, code: str) -> dict:
    row = connection.execute("SELECT * FROM roles WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ApiError(404, "ROLE_NOT_FOUND", "角色不存在")
    return {
        **dict(row),
        "built_in": bool(row["built_in"]),
        "permissions": role_permissions(connection, code),
        "user_count": connection.execute(
            """SELECT count(*) FROM user_roles ur JOIN users u ON u.id=ur.user_id
            WHERE ur.role_code=? AND u.deleted_at IS NULL""",
            (code,),
        ).fetchone()[0],
    }


def assign_roles(connection: Connection, user_id: str, codes: list[str]) -> None:
    require_roles(connection, codes)
    connection.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
    connection.executemany(
        "INSERT INTO user_roles(user_id,role_code) VALUES(?,?)",
        [(user_id, code) for code in codes],
    )


def assign_permissions(
    connection: Connection, code: str, permissions: list[str]
) -> None:
    require_permissions(connection, permissions)
    connection.execute("DELETE FROM role_permissions WHERE role_code=?", (code,))
    connection.executemany(
        "INSERT INTO role_permissions(role_code,permission_code) VALUES(?,?)",
        [(code, permission) for permission in permissions],
    )
