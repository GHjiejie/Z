"""Public execution progress, distinct from private prompts and graph checkpoints."""

import asyncio
import json
from collections.abc import Awaitable, Callable

from langchain_core.callbacks import AsyncCallbackHandler

from agent_permissions.hitl_forms import scrub

Progress = Callable[[dict], Awaitable[None]]


def display_value(value: object) -> object:
    clean = scrub(value)
    serialized = json.dumps(clean, ensure_ascii=False, default=str)
    return (
        clean
        if len(serialized) <= 16000
        else {"truncated": True, "preview": serialized[:16000]}
    )


def reasoning_text(message) -> str:
    """Only provider-exposed reasoning/summary text; never synthesize hidden reasoning."""
    pieces = []
    try:
        blocks = getattr(message, "content_blocks", [])
    except (TypeError, ValueError, KeyError):
        blocks = getattr(message, "content", [])
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and block.get("type") in {"reasoning", "thinking"}:
            value = block.get("reasoning") or block.get("thinking") or block.get("text")
            if isinstance(value, str):
                pieces.append(value)
            summaries = block.get("summary")
            for summary in summaries if isinstance(summaries, list) else []:
                if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                    pieces.append(summary["text"])
    if not pieces:
        value = getattr(message, "additional_kwargs", {}).get("reasoning_content")
        if isinstance(value, str):
            pieces.append(value)
    return "".join(pieces)


class ReasoningProgress(AsyncCallbackHandler):
    def __init__(self, entry_id: str, progress: Progress) -> None:
        self.entry_id, self.progress = entry_id, progress
        self.text = ""
        self.next_update = 0.0

    async def on_llm_new_token(self, token: str, *, chunk=None, **kwargs) -> None:
        value = reasoning_text(getattr(chunk, "message", None))
        if not value:
            return
        self.text = (self.text + value)[:64000]
        now = asyncio.get_running_loop().time()
        if now >= self.next_update:
            await self.progress({"id": self.entry_id, "reasoning": self.text})
            self.next_update = now + 0.15
