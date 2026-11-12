"""Isolated, temporary real API used only by the Console browser tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.security import token_digest
from agent_permissions.service import Authority, RequestInfo

ADMIN_PASSWORD = "12345"
MEMBER_PASSWORD = "Console member password 2026!"
INITIAL_PASSWORD = "12345"


def main() -> None:
    with TemporaryDirectory(prefix="agent-console-browser-") as temporary:
        settings = Settings(
            database_path=Path(temporary) / "browser.sqlite3",
            login_account_limit=1000,
            login_source_limit=1000,
            password_limit=1000,
        )
        app = create_app(settings)
        app.state.database.initialize()
        service = app.state.service
        service.bootstrap_admin(
            UserCreate(
                username="console_admin",
                display_name="系统管理员",
                password=ADMIN_PASSWORD,
            )
        )
        login = service.login(
            "console_admin",
            ADMIN_PASSWORD,
            RequestInfo("browser-test-fixture", "local-test"),
        )
        identity = Authority(token_digest(login["session_token"]))
        for username, name, role in (
            ("console_member", "普通成员", "member"),
            ("first_login", "新加入的成员", "member"),
            ("role_admin", "权限管理员", "admin"),
        ):
            service.create_user(
                identity,
                UserCreate(
                    username=username,
                    display_name=name,
                    password=MEMBER_PASSWORD
                    if username == "console_member"
                    else INITIAL_PASSWORD,
                    roles=[role],
                ),
                RequestInfo("browser-test-fixture", "local-test"),
            )
        uvicorn.run(
            app, host="127.0.0.1", port=18765, proxy_headers=False, access_log=False
        )


if __name__ == "__main__":
    main()
