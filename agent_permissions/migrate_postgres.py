"""Copy a consistent SQLite RBAC snapshot into an empty PostgreSQL database."""

import argparse
import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg

from agent_permissions.config import Settings
from agent_permissions.postgres import PostgresDatabase

TABLE_COLUMNS = {
    "permissions": ("code", "display_name", "resource"),
    "roles": (
        "code",
        "display_name",
        "description",
        "built_in",
        "version",
        "created_at",
        "updated_at",
    ),
    "users": (
        "id",
        "username",
        "display_name",
        "email",
        "password_hash",
        "status",
        "must_change_password",
        "version",
        "last_login_at",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "role_permissions": ("role_code", "permission_code"),
    "user_roles": ("user_id", "role_code"),
    "sessions": (
        "token_digest",
        "user_id",
        "scope",
        "expires_at",
        "created_at",
        "revoked_at",
    ),
    "audit_events": (
        "id",
        "actor_user_id",
        "target_user_id",
        "action",
        "outcome",
        "reason_code",
        "request_id",
        "source",
        "changes",
        "created_at",
    ),
    "rate_limits": ("bucket_key", "attempts", "expires_at"),
}


def fingerprint(rows: list[Mapping[str, Any]]) -> str:
    encoded = sorted(
        json.dumps(
            dict(row),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for row in rows
    )
    digest = hashlib.sha256()
    for row in encoded:
        digest.update(row.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def migrate(source_path: Path, target: PostgresDatabase) -> dict:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise ValueError("源 SQLite 文件不存在，未进行迁移")
    directory = source_path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = directory / f"{source_path.stem}-before-postgres-{uuid4().hex}.sqlite3"
    backup.touch(mode=0o600, exist_ok=False)
    with (
        closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(backup)) as snapshot,
    ):
        source.backup(snapshot)
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        if source.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("源数据库必须先升级为 RBAC schema 2，原文件与备份均已保留")
        if (
            source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or source.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("源数据库完整性检查失败，未导入数据")
        for table, columns in TABLE_COLUMNS.items():
            actual = tuple(
                row["name"] for row in source.execute(f"PRAGMA table_info({table})")
            )
            if actual != columns:
                raise ValueError(f"源数据库 {table} 字段与 schema 2 不一致，未导入数据")
        target.initialize(seed=False)
        report = {
            "source": str(source_path),
            "backup": str(backup),
            "target_backend": "postgresql",
            "tables": {},
        }
        with target.transaction(write=True) as destination:
            if any(
                destination.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in TABLE_COLUMNS
            ):
                raise ValueError("目标数据库已有数据，拒绝覆盖；请使用空数据库迁移")
            for table, columns in TABLE_COLUMNS.items():
                names = ",".join(columns)
                rows = source.execute(f"SELECT {names} FROM {table}").fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in columns)
                    destination.executemany(
                        f"INSERT INTO {table}({names}) VALUES({placeholders})",
                        [tuple(row[column] for column in columns) for row in rows],
                    )
                imported = destination.execute(
                    f"SELECT {names} FROM {table}"
                ).fetchall()
                expected = fingerprint(rows)
                if len(imported) != len(rows) or fingerprint(imported) != expected:
                    raise ValueError(f"{table} 迁移校验失败，已回滚全部导入")
                report["tables"][table] = {"rows": len(rows), "sha256": expected}
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="停写后将 SQLite 数据完整迁移到空 PostgreSQL 数据库"
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).parent / "deploy/.env"
    )
    args = parser.parse_args(argv)
    target: PostgresDatabase | None = None
    try:
        settings = Settings.from_env(args.env_file if args.env_file.is_file() else None)
        if not settings.database_url:
            print("缺少 AGENT_USERS_DATABASE_URL，请先生成部署配置")
            return 1
        target = PostgresDatabase(settings.database_url, settings=settings)
        target.open()
        report = migrate(args.source, target)
    except ValueError as error:
        print(str(error))
        return 1
    except (sqlite3.Error, psycopg.Error, OSError, RuntimeError):
        print(
            "迁移失败，原 SQLite 和已生成的备份保留；请检查数据库连接、字段内容和文件访问权限"
        )
        return 1
    finally:
        if target is not None:
            target.close()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    manifest = Path(report["backup"]).with_suffix(".migration.json")
    try:
        descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(encoded + "\n")
        print(f"迁移完成，逐表行数和 SHA-256 均一致。报告：{manifest}")
    except OSError:
        print("迁移已完成，报告文件未能保存；请保留以上终端校验结果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
