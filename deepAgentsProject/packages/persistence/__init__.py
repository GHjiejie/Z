from __future__ import annotations

from .database import Database


def create_database(location: str) -> Database:
    if location.startswith(("postgresql://", "postgres://")):
        from .postgres import PostgresDatabase

        return PostgresDatabase(location)
    return Database(location)


__all__ = ["Database", "create_database"]
