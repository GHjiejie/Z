"""Generate local Docker database credentials without logging their values."""

import os
import secrets
from pathlib import Path


def main() -> int:
    path = Path(__file__).parent / "deploy/.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    content = (
        "AGENT_USERS_POSTGRES_DB=agent_users\n"
        "AGENT_USERS_POSTGRES_USER=agent_users\n"
        "AGENT_USERS_POSTGRES_PORT=55432\n"
        f"AGENT_USERS_POSTGRES_PASSWORD={password}\n"
        f"AGENT_USERS_DATABASE_URL=postgresql://agent_users:{password}@127.0.0.1:55432/agent_users\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"配置已存在，保留现有数据库凭据：{path}")
        return 0
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    print(f"已生成私有数据库配置：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
