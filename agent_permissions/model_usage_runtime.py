"""Capture Responses usage per upstream request, including terminal SSE events."""

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from typing import Self

import psycopg
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessageChunk
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk
from starlette.concurrency import run_in_threadpool

from agent_permissions.errors import ApiError
from agent_permissions.model_usage import (
    confirm_model_call,
    confirmed_counts,
    start_model_call,
    token_count,
)
from agent_permissions.storage import DatabaseBackend

logger = logging.getLogger(__name__)


def _detail_count(details: Mapping[str, object], key: str) -> int | None:
    for name in (key, f"priority_{key}", f"flex_{key}"):
        value = token_count(details.get(name))
        if value is not None:
            return value
    return None


class ModelUsageMeter(AsyncCallbackHandler):
    run_inline = True
    raise_error = True

    def __init__(self, database: DatabaseBackend, call_id: str) -> None:
        self.database = database
        self.call_id = call_id
        self.recorded = False

    @classmethod
    async def start(
        cls,
        database: DatabaseBackend,
        *,
        user_id: str,
        run_id: str,
        model: str,
        started_at: float,
    ) -> Self:
        try:
            call_id = await run_in_threadpool(
                start_model_call,
                database,
                user_id=user_id,
                run_id=run_id,
                model=model,
                started_at=started_at,
            )
        except (sqlite3.Error, psycopg.Error):
            raise ApiError(
                503, "MODEL_USAGE_UNAVAILABLE", "模型用量暂时无法记录"
            ) from None
        return cls(database, call_id)

    async def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        **kwargs: object,
    ) -> None:
        if (
            isinstance(chunk, ChatGenerationChunk)
            and isinstance(chunk.message, AIMessageChunk)
            and chunk.message.chunk_position == "last"
        ):
            await self.record(chunk.message.usage_metadata)

    async def record(self, usage: UsageMetadata | None) -> None:
        if self.recorded or usage is None:
            return
        counts = confirmed_counts(
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("total_tokens"),
        )
        if counts is None:
            return

        async def save() -> None:
            try:
                await run_in_threadpool(
                    confirm_model_call,
                    self.database,
                    self.call_id,
                    counts,
                    _detail_count(usage.get("input_token_details") or {}, "cache_read"),
                    _detail_count(usage.get("output_token_details") or {}, "reasoning"),
                )
            except (sqlite3.Error, psycopg.Error) as error:
                logger.warning(
                    "Model usage update failed call_id=%s error_type=%s",
                    self.call_id,
                    type(error).__name__,
                )
                raise ApiError(
                    503, "MODEL_USAGE_UNAVAILABLE", "模型用量暂时无法记录"
                ) from None

        # The upstream already supplied usage. Finish this write even if the SSE client leaves.
        write = asyncio.create_task(save())
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            with suppress(ApiError):
                await write
            raise
        self.recorded = True
