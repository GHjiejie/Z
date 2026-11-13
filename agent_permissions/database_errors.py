"""Classify database availability without logging connection strings or SQL."""

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests


def connection_failure_reason(error: BaseException) -> str | None:
    for kind, reason in (
        (PoolTimeout, "DB_POOL_TIMEOUT"),
        (TooManyRequests, "DB_POOL_QUEUE_FULL"),
        (PoolClosed, "DB_POOL_CLOSED"),
    ):
        if isinstance(error, kind):
            return reason
    if isinstance(error, psycopg.OperationalError):
        state = error.sqlstate
        if (
            state is None
            or state.startswith(("08", "28"))
            or state
            in {
                "57P01",
                "57P02",
                "57P03",
            }
        ):
            return "DB_CONNECTION_UNAVAILABLE"
    return None
