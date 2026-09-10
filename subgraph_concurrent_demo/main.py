"""A first-class, concurrent LangGraph subgraph orchestration demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from chat_models.chat import chat_model
from subgraph_concurrent_demo.models import (
    AuditEvent,
    BranchName,
    CandidateSelection,
    ExpertInput,
    ExpertOutput,
    ExpertState,
    IntakeInput,
    IntakeOutput,
    IntakeState,
    Proposal,
    ServiceOption,
    SynthesisInput,
    SynthesisOutput,
    SynthesisState,
    TravelPlan,
    TravelRequest,
    TripInput,
    TripState,
)
from subgraph_concurrent_demo.services import (
    ServiceUnavailable,
    SimulatedTravelServices,
)

BRANCH_LABELS: dict[BranchName, str] = {
    "transport": "交通",
    "hotel": "住宿",
    "activity": "活动",
}
BRANCH_ORDER: dict[BranchName, int] = {
    "transport": 0,
    "hotel": 1,
    "activity": 2,
}


def build_intake_subgraph():
    """Normalize shared input while keeping its intermediate value private."""

    def normalize_request(state: IntakeState) -> dict:
        request = state["request"]
        normalized = request.model_copy(
            update={
                "origin": request.origin.strip(),
                "destination": request.destination.strip(),
                "interests": list(
                    dict.fromkeys(
                        interest.strip()
                        for interest in request.interests
                        if interest.strip()
                    )
                ),
            }
        )
        return {
            "normalized_request": normalized,
            "audit_events": [
                AuditEvent(
                    branch="intake",
                    stage="normalize",
                    status="completed",
                    message="旅行需求已规范化",
                )
            ],
        }

    def publish_request(state: IntakeState) -> dict:
        return {"request": state["normalized_request"]}

    builder = StateGraph(
        IntakeState,
        input_schema=IntakeInput,
        output_schema=IntakeOutput,
    )
    builder.add_node("normalize_request", normalize_request)
    builder.add_node("publish_request", publish_request)
    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "publish_request")
    builder.add_edge("publish_request", END)
    return builder.compile(name="intake_subgraph")


SearchFunction = Callable[[TravelRequest], Awaitable[list[ServiceOption]]]
RankingFunction = Callable[
    [BranchName, TravelRequest, list[ServiceOption]],
    Awaitable[ServiceOption],
]


candidate_selection_model = chat_model.with_structured_output(
    CandidateSelection,
    method="function_calling",
    tool_choice="auto",
)


async def select_candidate_with_model(
    branch: BranchName,
    request: TravelRequest,
    candidates: list[ServiceOption],
) -> ServiceOption:
    """Use the configured chat model to choose one exact service candidate."""

    label = BRANCH_LABELS[branch]
    result = await candidate_selection_model.ainvoke(
        [
            (
                "system",
                (
                    f"你是旅行规划中的{label}专家。必须调用 CandidateSelection 工具返回结果。"
                    "只能选择 candidates 数组中已有的一个方案，selected_index 从 0 开始。"
                    "综合用户需求、预算、候选价格、评分和详情做选择，不得虚构新方案或修改价格。"
                    "rationale 使用简洁中文。"
                ),
            ),
            (
                "human",
                json.dumps(
                    {
                        "request": request.model_dump(),
                        "candidates": [item.model_dump() for item in candidates],
                    },
                    ensure_ascii=False,
                ),
            ),
        ]
    )
    if result.selected_index >= len(candidates):
        raise ValueError(
            f"模型为 {branch} 返回无效候选下标 {result.selected_index}，"
            f"候选数量为 {len(candidates)}"
        )

    selected = candidates[result.selected_index]
    return selected.model_copy(
        update={
            "details": [
                *selected.details,
                f"模型选择理由：{result.rationale}",
            ]
        }
    )


def build_expert_subgraph(
    branch: BranchName,
    search: SearchFunction,
    ranker: RankingFunction = select_candidate_with_model,
):
    """Build one expert with private state and a shared output contract."""

    label = BRANCH_LABELS[branch]

    def mark_started(_: ExpertState) -> dict:
        return {
            "audit_events": [
                AuditEvent(
                    branch=branch,
                    stage="search",
                    status="started",
                    message=f"{label}分支开始查询",
                )
            ]
        }

    async def load_candidates(state: ExpertState) -> dict:
        try:
            candidates = await search(state["request"])
        except ServiceUnavailable as exc:
            return {
                "branch_error": str(exc),
                "audit_events": [
                    AuditEvent(
                        branch=branch,
                        stage="search",
                        status="failed",
                        message=str(exc),
                    )
                ],
            }
        return {
            "candidates": candidates,
            "audit_events": [
                AuditEvent(
                    branch=branch,
                    stage="search",
                    status="completed",
                    message=f"获得 {len(candidates)} 个{label}候选项",
                )
            ],
        }

    async def rank_candidates(state: ExpertState) -> dict:
        if state.get("branch_error"):
            return {}
        candidates = state.get("candidates", [])
        if not candidates:
            return {"branch_error": f"{branch} service returned no candidates"}
        selected = await ranker(branch, state["request"], candidates)
        return {
            "selected": selected,
            "audit_events": [
                AuditEvent(
                    branch=branch,
                    stage="model_selection",
                    status="completed",
                    message=f"模型已选定{label}方案：{selected.title}",
                )
            ],
        }

    def publish_proposal(state: ExpertState) -> dict:
        error = state.get("branch_error")
        if error:
            proposal = Proposal(
                branch=branch,
                status="degraded",
                title=f"{label}服务暂不可用",
                estimated_cost=0,
                score=0,
                details=[error],
            )
            return {
                "proposals": [proposal],
                "warnings": [f"{label}分支已降级：{error}"],
            }

        selected = state["selected"]
        proposal = Proposal(
            branch=branch,
            status="ok",
            title=selected.title,
            estimated_cost=selected.estimated_cost,
            score=selected.score,
            details=selected.details,
        )
        return {"proposals": [proposal]}

    builder = StateGraph(
        ExpertState,
        input_schema=ExpertInput,
        output_schema=ExpertOutput,
    )
    builder.add_node("mark_started", mark_started)
    builder.add_node("load_candidates", load_candidates)
    builder.add_node("rank_candidates", rank_candidates)
    builder.add_node("publish_proposal", publish_proposal)
    builder.add_edge(START, "mark_started")
    builder.add_edge("mark_started", "load_candidates")
    builder.add_edge("load_candidates", "rank_candidates")
    builder.add_edge("rank_candidates", "publish_proposal")
    builder.add_edge("publish_proposal", END)
    return builder.compile(name=f"{branch}_subgraph")


def build_synthesis_subgraph():
    """Join all expert results, validate them, and create one final plan."""

    def order_and_validate(state: SynthesisState) -> dict:
        ordered = sorted(state["proposals"], key=lambda item: BRANCH_ORDER[item.branch])
        present = {proposal.branch for proposal in ordered}
        missing = [branch for branch in BRANCH_ORDER if branch not in present]
        derived = [f"缺少 {BRANCH_LABELS[branch]} 分支结果" for branch in missing]
        return {
            "ordered_proposals": ordered,
            "derived_warnings": derived,
        }

    def create_plan(state: SynthesisState) -> dict:
        proposals = state["ordered_proposals"]
        available = [item for item in proposals if item.status == "ok"]
        total_cost = sum(item.estimated_cost for item in available)
        warnings = [*state["warnings"], *state.get("derived_warnings", [])]
        remaining = state["request"].budget - total_cost
        if remaining < 0:
            warnings.append(f"当前组合超出预算 {-remaining} 元")

        if not available:
            status = "failed"
        elif len(available) < len(BRANCH_ORDER):
            status = "partial"
        else:
            status = "complete"

        summary = (
            f"已生成 {len(available)}/{len(BRANCH_ORDER)} 个可用模块，"
            f"预计总费用 {total_cost} 元。"
        )
        return {
            "final_plan": TravelPlan(
                status=status,
                proposals=proposals,
                total_cost=total_cost,
                remaining_budget=remaining,
                warnings=warnings,
                summary=summary,
            ),
            "audit_events": [
                AuditEvent(
                    branch="synthesis",
                    stage="create_plan",
                    status="completed",
                    message="所有并发分支已汇总",
                )
            ],
        }

    builder = StateGraph(
        SynthesisState,
        input_schema=SynthesisInput,
        output_schema=SynthesisOutput,
    )
    builder.add_node("order_and_validate", order_and_validate)
    builder.add_node("create_plan", create_plan)
    builder.add_edge(START, "order_and_validate")
    builder.add_edge("order_and_validate", "create_plan")
    builder.add_edge("create_plan", END)
    return builder.compile(name="synthesis_subgraph")


def format_result(state: TripState) -> dict[str, str]:
    plan = state["final_plan"]
    lines = [
        f"方案状态：{plan.status}",
        plan.summary,
        f"预算余额：{plan.remaining_budget} 元",
    ]
    for proposal in plan.proposals:
        lines.append(
            f"\n[{BRANCH_LABELS[proposal.branch]} / {proposal.status}] "
            f"{proposal.title}（{proposal.estimated_cost} 元，评分 {proposal.score}）"
        )
        lines.extend(f"  - {detail}" for detail in proposal.details)
    lines.extend(f"\n提示：{warning}" for warning in plan.warnings)
    return {"final_answer": "\n".join(lines)}


def build_graph(
    services: SimulatedTravelServices | None = None,
    ranker: RankingFunction = select_candidate_with_model,
):
    """Compile the parent graph with first-class subgraph nodes."""

    resolved_services = services or SimulatedTravelServices()
    intake_subgraph = build_intake_subgraph()
    transport_subgraph = build_expert_subgraph(
        "transport", resolved_services.search_transport, ranker
    )
    hotel_subgraph = build_expert_subgraph(
        "hotel", resolved_services.search_hotels, ranker
    )
    activity_subgraph = build_expert_subgraph(
        "activity", resolved_services.search_activities, ranker
    )
    synthesis_subgraph = build_synthesis_subgraph()

    builder = StateGraph(TripState, input_schema=TripInput)
    builder.add_node("intake", intake_subgraph)
    builder.add_node("transport", transport_subgraph)
    builder.add_node("hotel", hotel_subgraph)
    builder.add_node("activity", activity_subgraph)
    builder.add_node("synthesis", synthesis_subgraph)
    builder.add_node("finalize", format_result)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "transport")
    builder.add_edge("intake", "hotel")
    builder.add_edge("intake", "activity")
    builder.add_edge(["transport", "hotel", "activity"], "synthesis")
    builder.add_edge("synthesis", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(name="concurrent_travel_planner")


def default_request() -> TravelRequest:
    return TravelRequest(
        origin="上海",
        destination="杭州",
        days=3,
        travelers=2,
        budget=3500,
        interests=["人文", "美食", "人文"],
    )


def describe_update(namespace: tuple[str, ...], update: dict) -> str:
    scope = " > ".join(part.split(":", 1)[0] for part in namespace) or "parent"
    parts = []
    for node, payload in update.items():
        keys = ", ".join(payload) if isinstance(payload, dict) else "no public output"
        parts.append(f"{node} ({keys})")
    return f"{scope}: " + "; ".join(parts)


async def run_demo(fail_branches: frozenset[BranchName]) -> None:
    graph = build_graph(SimulatedTravelServices(fail_branches=fail_branches))
    started = time.perf_counter()
    final_answer = ""

    print("父图：")
    print(graph.get_graph().draw_ascii())
    print("\n实时更新（三个专家分支会并发调用真实模型）：")
    async for namespace, update in graph.astream(
        {"request": default_request()},
        stream_mode="updates",
        subgraphs=True,
    ):
        elapsed = time.perf_counter() - started
        print(f"[{elapsed:5.2f}s] {describe_update(namespace, update)}")
        if not namespace and "finalize" in update:
            final_answer = update["finalize"]["final_answer"]

    print("\n最终结果：\n")
    print(final_answer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail",
        action="append",
        choices=tuple(BRANCH_ORDER),
        default=[],
        help="模拟指定外部服务失败；可重复传入",
    )
    args = parser.parse_args()
    asyncio.run(run_demo(frozenset(args.fail)))


if __name__ == "__main__":
    main()
