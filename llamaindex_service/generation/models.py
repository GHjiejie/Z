"""True asynchronous LangChainLLM adapter preserving Responses text and usage."""

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from llama_index.core.base.llms.types import ChatMessage, ChatResponse, LLMMetadata
from llama_index.llms.langchain import LangChainLLM


def content_text(content: Any) -> str:
    """Only user-visible text; reasoning/tool/image blocks are never answers."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in {"text", "output_text"}
            and isinstance(part.get("text"), str)
        )
    return ""


def message_usage(message: Any) -> dict | None:
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        metadata = getattr(message, "response_metadata", {}) or {}
        usage = metadata.get("token_usage") or metadata.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


def _messages(messages: Sequence[ChatMessage]) -> list:
    types = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [
        types[message.role.value](content=message.content or "") for message in messages
    ]


class AsyncLangChainLLM(LangChainLLM):
    """The upstream adapter uses blocking sync fallback and drops usage.

    Override only async methods; preserve real cancellation through LangChain's
    async generator and retain model-provided usage without estimating counts.
    """

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            is_chat_model=True,
            model_name=getattr(self.llm, "model_name", "configured-chat-model"),
        )

    async def achat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponse:
        response = await self.llm.ainvoke(_messages(messages), **kwargs)
        return ChatResponse(
            message=ChatMessage(
                role="assistant", content=content_text(response.content)
            ),
            additional_kwargs={"usage": message_usage(response)},
        )

    async def astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> AsyncIterator[ChatResponse]:
        async def generate() -> AsyncIterator[ChatResponse]:
            text = ""
            stream = self.llm.astream(_messages(messages), **kwargs)
            try:
                async for message in stream:
                    delta = content_text(message.content)
                    text += delta
                    yield ChatResponse(
                        message=ChatMessage(role="assistant", content=text),
                        delta=delta,
                        additional_kwargs={"usage": message_usage(message)},
                    )
            finally:
                if hasattr(stream, "aclose"):
                    await stream.aclose()

        return generate()


class LiveGenerator:
    def __init__(self, llm: Any = None):
        self._llm = llm

    def _adapter(self) -> AsyncLangChainLLM:
        if self._llm is None:
            from chat_models.chat import chat_model

            self._llm = chat_model
        return AsyncLangChainLLM(llm=self._llm)

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[dict]:
        stream = await self._adapter().astream_chat(messages)
        try:
            async for chunk in stream:
                yield {
                    "delta": chunk.delta or "",
                    "usage": chunk.additional_kwargs.get("usage"),
                }
        finally:
            await stream.aclose()


class MockGenerator:
    """Deterministic excerpt display exercises plumbing, never answer quality."""

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[dict]:
        # Evidence is a separate user message authored by the service. It is
        # JSON so malicious text cannot create another application message.
        import json

        evidence = json.loads(messages[-2].content)["evidence"]
        excerpt = evidence[0]["text"][:400]
        answer = f"开发模式摘录（未调用生成模型）：{excerpt} [1]"
        for offset in range(0, len(answer), 32):
            yield {"delta": answer[offset : offset + 32], "usage": None}
