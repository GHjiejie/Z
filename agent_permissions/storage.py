"""Small SQL boundary shared by SQLite and PostgreSQL adapters."""

from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol


class QueryResult(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


class Connection(Protocol):
    def execute(
        self, statement: str, parameters: Sequence[Any] | None = None
    ) -> QueryResult: ...

    def executemany(
        self, statement: str, parameters: Iterable[Sequence[Any]]
    ) -> QueryResult: ...


class DatabaseBackend(Protocol):
    def initialize(self) -> None: ...

    def connect(self) -> AbstractContextManager[Connection]: ...

    def transaction(
        self, *, write: bool = False
    ) -> AbstractContextManager[Connection]: ...
