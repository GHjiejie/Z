"""LangGraph checkpointer using the application's SQLite/PostgreSQL SQL boundary."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from agent_permissions.storage import DatabaseBackend


class DatabaseCheckpointer(BaseCheckpointSaver[int]):
    """Persist full checkpoints and pending writes without holding borrowed connections."""

    def __init__(self, database: DatabaseBackend) -> None:
        super().__init__()
        self.database = database

    def _encode(self, value: Any) -> tuple[str, str]:
        kind, data = self.serde.dumps_typed(value)
        return kind, base64.b64encode(data).decode("ascii")

    def _decode(self, kind: str, data: str) -> Any:
        return self.serde.loads_typed((kind, base64.b64decode(data)))

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        values = config["configurable"]
        thread, namespace = values["thread_id"], values.get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM agent_checkpoints WHERE thread_id=? AND namespace=?"
                + (" AND checkpoint_id=?" if checkpoint_id else "")
                + " ORDER BY checkpoint_id DESC LIMIT 1",
                (thread, namespace, checkpoint_id)
                if checkpoint_id
                else (thread, namespace),
            ).fetchone()
            if not row:
                return None
            writes = connection.execute(
                "SELECT * FROM agent_checkpoint_writes WHERE thread_id=? AND namespace=? AND checkpoint_id=? ORDER BY task_id,idx",
                (thread, namespace, row["checkpoint_id"]),
            ).fetchall()
            base = {"thread_id": thread, "checkpoint_ns": namespace}
            return CheckpointTuple(
                config={
                    "configurable": {**base, "checkpoint_id": row["checkpoint_id"]}
                },
                checkpoint=self._decode(row["kind"], row["checkpoint"]),
                metadata=json.loads(row["metadata"]),
                parent_config={
                    "configurable": {**base, "checkpoint_id": row["parent_id"]}
                }
                if row["parent_id"]
                else None,
                pending_writes=[
                    (
                        item["task_id"],
                        item["channel"],
                        self._decode(item["kind"], item["value"]),
                    )
                    for item in writes
                ],
            )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        values = config["configurable"]
        thread, namespace = values["thread_id"], values.get("checkpoint_ns", "")
        kind, encoded = self._encode(checkpoint)
        with self.database.transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO agent_checkpoints(thread_id,namespace,checkpoint_id,parent_id,kind,checkpoint,metadata)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(thread_id,namespace,checkpoint_id) DO UPDATE SET
                checkpoint=excluded.checkpoint,metadata=excluded.metadata,kind=excluded.kind""",
                (
                    thread,
                    namespace,
                    checkpoint["id"],
                    values.get("checkpoint_id"),
                    kind,
                    encoded,
                    json.dumps(
                        get_checkpoint_metadata(config, metadata), ensure_ascii=False
                    ),
                ),
            )
        return {
            "configurable": {
                "thread_id": thread,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        values = config["configurable"]
        with self.database.transaction(write=True) as connection:
            for index, (channel, value) in enumerate(writes):
                suffix = (
                    "DO UPDATE SET kind=excluded.kind,value=excluded.value"
                    if channel in WRITES_IDX_MAP
                    else "DO NOTHING"
                )
                connection.execute(
                    "INSERT INTO agent_checkpoint_writes(thread_id,namespace,checkpoint_id,task_id,idx,channel,kind,value) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(thread_id,namespace,checkpoint_id,task_id,idx) "
                    + suffix,
                    (
                        values["thread_id"],
                        values.get("checkpoint_ns", ""),
                        values["checkpoint_id"],
                        task_id,
                        WRITES_IDX_MAP.get(channel, index),
                        channel,
                        *self._encode(value),
                    ),
                )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            return
        values = config["configurable"]
        with self.database.transaction() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT checkpoint_id FROM agent_checkpoints WHERE thread_id=? AND namespace=? ORDER BY checkpoint_id DESC",
                    (values["thread_id"], values.get("checkpoint_ns", "")),
                )
            ]
        count = 0
        for identity in ids:
            if before and identity >= get_checkpoint_id(before):
                continue
            item = self.get_tuple(
                {"configurable": {**values, "checkpoint_id": identity}}
            )
            if item and all(
                item.metadata.get(k) == v for k, v in (filter or {}).items()
            ):
                if limit is not None and count >= limit:
                    break
                yield item
                count += 1

    def delete_thread(self, thread_id: str) -> None:
        with self.database.transaction(write=True) as connection:
            for table in ("agent_checkpoint_writes", "agent_checkpoints"):
                connection.execute(
                    f"DELETE FROM {table} WHERE thread_id=?", (thread_id,)
                )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
