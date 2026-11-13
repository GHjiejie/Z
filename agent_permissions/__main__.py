"""Local, one-time administrator initialization with a default password."""

import argparse
import getpass
import sqlite3
from collections.abc import Callable
from pathlib import Path

import psycopg
from pydantic import ValidationError

from agent_permissions.config import Settings
from agent_permissions.database import create_database
from agent_permissions.errors import ApiError
from agent_permissions.models import UserCreate
from agent_permissions.service import UserService

INITIAL_ADMIN_PASSWORD = "12345"


def main(
    argv: list[str] | None = None,
    *,
    password_reader: Callable[[str], str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Agent 用户管理服务维护入口")
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init-admin", help="一次性创建初始管理员")
    initialize.add_argument("--username", required=True)
    initialize.add_argument("--display-name", default="管理员")
    initialize.add_argument("--email")
    initialize.add_argument("--env-file", type=Path, help="读取本模块的数据库配置文件")
    initialize.add_argument(
        "--prompt-password", action="store_true", help="交互式设置自定义初始密码"
    )
    args = parser.parse_args(argv)
    try:
        password = INITIAL_ADMIN_PASSWORD
        if args.prompt_password:
            password_reader = password_reader or getpass.getpass
            password = password_reader("请输入初始密码：")
            confirmation = password_reader("请再次输入初始密码：")
            if password != confirmation:
                print("两次密码不一致，未创建用户。")
                return 1
        payload = UserCreate(
            username=args.username,
            display_name=args.display_name,
            email=args.email,
            password=password,
            roles=["admin"],
        )
        settings = Settings.from_env(args.env_file)
        database = create_database(settings)
        database.initialize()
        user = UserService(database, settings).bootstrap_admin(payload)
        print(f"已创建管理员 {user['username']}，可直接登录管理后台。")
        return 0
    except ValidationError as error:
        print("初始化参数不符合要求：")
        for detail in error.errors():
            print(detail["msg"])
    except ApiError as error:
        print(error.message)
    except (sqlite3.Error, psycopg.Error, OSError, ValueError):
        print("初始化失败，请检查配置和数据库访问权限。")
    except (EOFError, KeyboardInterrupt):
        print("已取消初始化。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
