"""Dependency readiness without provider calls, credentials or tenant labels."""

import asyncio
import time
from contextlib import suppress
from uuid import uuid4

from sqlalchemy import delete, insert, select, update

from agent_platform.infrastructure.runtime_tables import service_heartbeats


async def run_service(db, role, service, stop):
    identifier = uuid4().hex

    def beat():
        with db.transaction("health:" + role) as connection:
            connection.execute(
                delete(service_heartbeats).where(
                    service_heartbeats.c.expires_at < time.time() - 300
                )
            )
            exists = connection.scalar(
                select(service_heartbeats.c.id).where(
                    service_heartbeats.c.id == identifier
                )
            )
            if exists:
                connection.execute(
                    update(service_heartbeats)
                    .where(service_heartbeats.c.id == identifier)
                    .values(expires_at=time.time() + 30)
                )
            else:
                connection.execute(
                    insert(service_heartbeats).values(
                        id=identifier, role=role, expires_at=time.time() + 30
                    )
                )

    async def pulse():
        while not stop.is_set():
            await asyncio.to_thread(beat)
            await asyncio.sleep(5)

    heartbeat = asyncio.create_task(pulse())
    operation = asyncio.create_task(service(stop))
    try:
        done, _pending = await asyncio.wait(
            [heartbeat, operation], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            await task
    finally:
        heartbeat.cancel()
        operation.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        with suppress(asyncio.CancelledError):
            await operation
        with db.transaction("health:" + role) as connection:
            connection.execute(
                delete(service_heartbeats).where(service_heartbeats.c.id == identifier)
            )


def readiness(db, settings):
    db.assert_schema(saas=settings.mode == "saas")
    with db.read() as connection:
        live = set(
            connection.scalars(
                select(service_heartbeats.c.role).where(
                    service_heartbeats.c.expires_at > time.time()
                )
            )
        )
    required = {"worker", "maintenance"}
    if settings.mode == "saas":
        required.add("gateway-sync")
    missing = sorted(required - live)
    if settings.redis_url:
        from redis import Redis

        client = Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            client.ping()
        finally:
            client.close()
    return {
        "status": "ready" if not missing else "degraded",
        "missing_services": missing,
    }
