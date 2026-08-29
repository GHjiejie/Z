from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Literal

from packages.knowledge.tool import KnowledgeSearchTool


RAGRoute = Literal["knowledge", "model_only"]


@dataclass(frozen=True)
class RAGAgentResult:
    route: RAGRoute
    reason: str
    query: str
    retrieval: Dict[str, Any]
    tool_calls: int


class BuiltinRAGAgent:
    """Built-in evidence router and retriever used by the runtime.

    The agent never decides authorization itself. It receives a trusted runtime
    principal, validates the plan's immutable knowledge bindings through the
    search tool, and routes to model-only generation when no reliable evidence
    is available.
    """

    name = "builtin_rag"
    version = "1.0.0"

    def __init__(self, tool: KnowledgeSearchTool):
        self.tool = tool

    def should_probe(
        self,
        query: str,
        plan: Dict[str, Any],
        prior_user_messages: List[str] | None = None,
    ) -> bool:
        bindings = plan.get("knowledge_bindings", [])
        allowed_tools = {
            binding.get("name") for binding in plan.get("tool_bindings", [])
        }
        return bool(
            bindings
            and "knowledge_search" in allowed_tools
            and self._needs_knowledge_probe(
                self._contextualize(query, prior_user_messages or [])
            )
        )

    async def run(
        self,
        query: str,
        plan: Dict[str, Any],
        runtime_context: Dict[str, Any],
        *,
        prior_user_messages: List[str] | None = None,
    ) -> RAGAgentResult:
        bindings = plan.get("knowledge_bindings", [])
        allowed_tools = {
            binding.get("name") for binding in plan.get("tool_bindings", [])
        }
        if not bindings or "knowledge_search" not in allowed_tools:
            return RAGAgentResult(
                route="model_only",
                reason="knowledge_search_not_bound",
                query=query,
                retrieval={
                    "status": "not_requested",
                    "hits": [],
                    "revision_ids": [],
                },
                tool_calls=0,
            )
        prior_user_messages = prior_user_messages or []
        effective_query = self._contextualize(query, prior_user_messages)
        if not self.should_probe(query, plan, prior_user_messages):
            return RAGAgentResult(
                route="model_only",
                reason="request_is_model_native",
                query=query,
                retrieval={
                    "status": "not_requested",
                    "hits": [],
                    "revision_ids": [binding["revision_id"] for binding in bindings],
                },
                tool_calls=0,
            )

        profiles = [binding["retrieval_profile"] for binding in bindings]
        top_k = min(int(profile["default_top_k"]) for profile in profiles)
        retrieval = await asyncio.to_thread(
            self.tool.invoke,
            effective_query,
            plan,
            runtime_context,
            top_k=top_k,
        )
        routing_min_score = max(
            float(profile.get("routing_min_score", 0.04)) for profile in profiles
        )
        top_score = max(
            (float(hit.get("score", 0.0)) for hit in retrieval.get("hits", [])),
            default=0.0,
        )
        if not retrieval.get("hits") or top_score < routing_min_score:
            return RAGAgentResult(
                route="model_only",
                reason="no_reliable_knowledge_evidence",
                query=effective_query,
                retrieval={**retrieval, "status": "insufficient_evidence", "hits": []},
                tool_calls=1,
            )

        max_context = min(
            int(profile.get("max_context_characters", 24_000)) for profile in profiles
        )
        bounded_hits = self._limit_context(retrieval["hits"], max_context)
        return RAGAgentResult(
            route="knowledge",
            reason="reliable_knowledge_evidence_found",
            query=effective_query,
            retrieval={**retrieval, "hits": bounded_hits},
            tool_calls=1,
        )

    @staticmethod
    def _contextualize(query: str, prior_user_messages: List[str]) -> str:
        normalized = query.strip()
        ambiguous_markers = (
            "这个",
            "那个",
            "上面",
            "前面",
            "第二",
            "它",
            "其",
            "that",
            "it ",
            "the second",
            "above",
            "previous",
        )
        if prior_user_messages and (
            len(normalized) <= 40
            or any(marker in normalized.lower() for marker in ambiguous_markers)
        ):
            return f"Previous request: {prior_user_messages[-1]}\nCurrent follow-up: {normalized}"
        return normalized

    @staticmethod
    def _needs_knowledge_probe(query: str) -> bool:
        normalized = query.strip().lower()
        model_native_markers = (
            "写一首",
            "写首",
            "创作",
            "改写",
            "翻译以下",
            "润色",
            "头脑风暴",
            "write a poem",
            "brainstorm",
            "rewrite this",
            "translate the following",
        )
        knowledge_markers = (
            "根据",
            "文档",
            "知识库",
            "项目",
            "规范",
            "流程",
            "政策",
            "手册",
            "内部",
            "当前",
            "发布",
            "部署",
            "为什么",
            "如何",
            "什么",
            "谁",
            "多少",
            "according to",
            "documentation",
            "project",
            "policy",
            "handbook",
            "current",
            "release",
            "deploy",
            "why",
            "how",
            "what",
            "who",
        )
        if any(marker in normalized for marker in knowledge_markers):
            return True
        if any(marker in normalized for marker in model_native_markers):
            return False
        return normalized.endswith(("?", "？"))

    @staticmethod
    def _limit_context(hits: List[Dict[str, Any]], max_characters: int) -> List[Dict[str, Any]]:
        remaining = max_characters
        bounded: List[Dict[str, Any]] = []
        for hit in hits:
            if remaining <= 0:
                break
            text = str(hit.get("text", ""))[:remaining]
            if not text:
                continue
            bounded.append({**hit, "text": text})
            remaining -= len(text)
        return bounded
