"""Three concurrent LangGraph subgraphs that require independent human review."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from chat_models.chat import chat_model
from subgraph_interrupt_demo.models import (
    AuditEvent,
    BranchName,
    GeneratedProposal,
    ParentInput,
    ParentState,
    ReviewDecision,
    ReviewProposal,
    ReviewRequest,
    ReviewResult,
    ReviewSubgraphInput,
    ReviewSubgraphOutput,
    ReviewSubgraphState,
    ReviewSummary,
)

BRANCH_ORDER: tuple[BranchName, ...] = ("content", "compliance", "delivery")
BRANCH_LABELS: dict[BranchName, str] = {
    "content": "内容审核",
    "compliance": "合规审核",
    "delivery": "发布审核",
}
BRANCH_INSTRUCTIONS: dict[BranchName, str] = {
    "content": (
        "你是内容编辑。基于用户提供的主题和原始内容，生成可以直接发布的中文文案。"
        "保持原意，不虚构事实；proposed_value 必须是完整的修改后文案。"
    ),
    "compliance": (
        "你是发布前合规审核员。识别原始内容中的敏感信息、未经证实的承诺、隐私和"
        "误导风险；proposed_value 必须给出明确审核结论及必要的修改建议，不做法律保证。"
    ),
    "delivery": (
        "你是发布运营审核员。结合内容、目标渠道和计划时间给出可执行的发布安排；"
        "proposed_value 必须包含建议渠道、发布时间和发布注意事项。"
    ),
}

ProposalBuilder = Callable[[BranchName, ReviewRequest], ReviewProposal]

# The default production path performs one real structured model call per subgraph.
generated_proposal_model = chat_model.with_structured_output(
    GeneratedProposal,
    method="function_calling",
    tool_choice="auto",
)


def checkpoint_serializer() -> JsonPlusSerializer:
    """Trust only this demo's explicit Pydantic state types during restore."""

    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ReviewRequest,
            ReviewProposal,
            ReviewDecision,
            ReviewResult,
            AuditEvent,
            ReviewSummary,
        ]
    )


def default_proposal_builder(
    branch: BranchName, request: ReviewRequest
) -> ReviewProposal:
    """Call the configured real model to generate one branch proposal."""

    generated = generated_proposal_model.invoke(
        [
            (
                "system",
                (
                    f"{BRANCH_INSTRUCTIONS[branch]}"
                    "必须调用 GeneratedProposal 工具返回结构化结果。"
                    "notes 列出需要人工判断的重点；没有重点时返回空列表。"
                ),
            ),
            (
                "human",
                "待审核发布请求：\n" + request.model_dump_json(indent=2),
            ),
        ]
    )
    result = GeneratedProposal.model_validate(generated)

    return ReviewProposal(
        branch=branch,
        title=result.title,
        proposed_value=result.proposed_value,
        notes=result.notes,
    )


def build_review_subgraph(
    branch: BranchName,
    proposal_builder: ProposalBuilder,
):
    """Compile one first-class review subgraph with a dynamic interrupt."""

    def build_proposal(state: ReviewSubgraphState) -> dict[str, Any]:
        proposal = proposal_builder(branch, state["request"])
        if proposal.branch != branch:
            raise ValueError(
                f"proposal_builder 为 {branch} 返回了错误分支 {proposal.branch}"
            )
        return {
            "proposal": proposal,
            "audit_events": [
                AuditEvent(
                    branch=branch,
                    stage="proposal_built",
                    message=f"{BRANCH_LABELS[branch]}方案已生成",
                )
            ],
        }

    def human_review(state: ReviewSubgraphState) -> dict[str, ReviewDecision]:
        proposal = state["proposal"]
        # The node is restarted on resume. Keep everything before interrupt pure.
        raw_decision = interrupt(
            {
                "kind": "human_review",
                "branch": branch,
                "title": proposal.title,
                "proposal": proposal.model_dump(mode="json"),
                "allowed_decisions": ["approve", "edit", "reject"],
            }
        )
        return {"decision": ReviewDecision.model_validate(raw_decision)}

    def apply_decision(state: ReviewSubgraphState) -> ReviewSubgraphOutput:
        proposal = state["proposal"]
        decision = state["decision"]
        if decision.type == "approve":
            status = "approved"
            final_value = proposal.proposed_value
            reason = decision.reason
        elif decision.type == "edit":
            status = "edited"
            final_value = decision.replacement.strip() if decision.replacement else None
            reason = decision.reason
        else:
            status = "rejected"
            final_value = None
            reason = decision.reason

        result = ReviewResult(
            branch=branch,
            status=status,
            original_value=proposal.proposed_value,
            final_value=final_value,
            reason=reason,
        )
        return {
            "review_results": [result],
            "audit_events": [
                AuditEvent(
                    branch=branch,
                    stage="human_decision_applied",
                    message=f"{BRANCH_LABELS[branch]}结果：{status}",
                )
            ],
        }

    builder = StateGraph(
        ReviewSubgraphState,
        input_schema=ReviewSubgraphInput,
        output_schema=ReviewSubgraphOutput,
    )
    builder.add_node("build_proposal", build_proposal)
    builder.add_node("human_review", human_review)
    builder.add_node("apply_decision", apply_decision)
    builder.add_edge(START, "build_proposal")
    builder.add_edge("build_proposal", "human_review")
    builder.add_edge("human_review", "apply_decision")
    builder.add_edge("apply_decision", END)
    # Omit checkpointer here: each invocation inherits the parent's checkpointer.
    return builder.compile(name=f"{branch}_review_subgraph")


def summarize_reviews(state: ParentState) -> dict[str, Any]:
    ordered = sorted(
        state.get("review_results", []),
        key=lambda item: BRANCH_ORDER.index(item.branch),
    )
    approved = sum(item.status == "approved" for item in ordered)
    edited = sum(item.status == "edited" for item in ordered)
    rejected = sum(item.status == "rejected" for item in ordered)
    ready = len(ordered) == len(BRANCH_ORDER) and rejected == 0
    message = (
        "三个审核均已通过，可以发布。" if ready else "审核未全部通过，本次发布已阻止。"
    )
    content_result = next(
        (item for item in ordered if item.branch == "content"),
        None,
    )
    # Downstream publishing must consume the post-review value, never the
    # original proposal retained for audit.
    publish_content = (
        content_result.final_value if ready and content_result is not None else None
    )
    return {
        "final_summary": ReviewSummary(
            ready_to_publish=ready,
            approved_count=approved,
            edited_count=edited,
            rejected_count=rejected,
            results=ordered,
            message=message,
            publish_content=publish_content,
        ),
        "audit_events": [
            AuditEvent(branch="parent", stage="summarized", message=message)
        ],
    }


def build_graph(
    checkpointer: BaseCheckpointSaver | None,
    proposal_builder: ProposalBuilder = default_proposal_builder,
):
    """Compile a parent graph containing exactly three parallel subgraphs."""

    # 建立需要review的三个子图
    subgraphs = {
        branch: build_review_subgraph(branch, proposal_builder)
        for branch in BRANCH_ORDER
    }

    builder = StateGraph(ParentState, input_schema=ParentInput)
    for branch, subgraph in subgraphs.items():
        builder.add_node(branch, subgraph)
        builder.add_edge(START, branch)

    builder.add_node("summarize", summarize_reviews)
    builder.add_edge(list(BRANCH_ORDER), "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(
        checkpointer=checkpointer,
        name="three_subgraph_human_review",
    )


def default_request() -> ReviewRequest:
    return ReviewRequest(
        topic="LangGraph 人工介入演示",
        content="我们计划发布一个包含三个并发审核环节的工作流示例。",
        target_channel="产品公告",
        scheduled_at="明天 10:00",
    )


def prompt_for_decision(payload: dict[str, Any]) -> ReviewDecision:
    proposal = ReviewProposal.model_validate(payload["proposal"])
    print(f"\n[{payload['title']}] {proposal.proposed_value}")
    for note in proposal.notes:
        print(f"  - {note}")
    while True:
        choice = input("请选择 approve / edit / reject：").strip().lower()
        raw: dict[str, str] = {"type": choice}
        if choice == "edit":
            raw["replacement"] = input("请输入修改后的内容：").strip()
            raw["reason"] = input("修改说明（可留空）：").strip()
        elif choice == "reject":
            raw["reason"] = input("请输入拒绝原因：").strip()
        elif choice == "approve":
            raw["reason"] = input("审批说明（可留空）：").strip()
        try:
            return ReviewDecision.model_validate(raw)
        except ValidationError as exc:
            print(f"输入无效：{exc.errors()[0]['msg']}")


def print_summary(summary: ReviewSummary) -> None:
    print("\n=== 最终审核结果 ===")
    print(summary.message)
    for result in summary.results:
        line = f"- {BRANCH_LABELS[result.branch]}：{result.status}"
        if result.final_value is not None:
            line += f"；最终值：{result.final_value}"
        if result.reason:
            line += f"；说明：{result.reason}"
        print(line)
    if summary.ready_to_publish:
        print(f"最终发布文案：{summary.publish_content}")


def run_cli(thread_id: str, database_path: Path, request: ReviewRequest) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(str(database_path), check_same_thread=False)
    ) as connection:
        checkpointer = SqliteSaver(connection, serde=checkpoint_serializer())
        # 结构化输出一下checkpointer的内容，看看里面都有哪些信息

        graph = build_graph(checkpointer)
        snapshot = graph.get_state(config)
        if snapshot.next:
            print(f"恢复待处理会话：{thread_id}")
            output = graph.invoke(None, config=config, version="v2")
        elif snapshot.values.get("final_summary"):
            print(f"会话 {thread_id} 已经完成；如需新审核，请使用新的 --thread-id。")
            print_summary(snapshot.values["final_summary"])
            return
        else:
            print(f"启动会话：{thread_id}")
            print("三个子图正在并发调用真实模型生成审核方案……")
            output = graph.invoke({"request": request}, config=config, version="v2")

        interrupts = sorted(
            output.interrupts,
            key=lambda item: BRANCH_ORDER.index(item.value["branch"]),
        )
        if len(interrupts) != len(BRANCH_ORDER):
            raise RuntimeError(f"预期 3 个 interrupt，实际得到 {len(interrupts)} 个")

        print("\n三个子图均已暂停，等待人工分别处理。")
        resume_values = {
            item.id: prompt_for_decision(item.value).model_dump(mode="json")
            for item in interrupts
        }
        completed = graph.invoke(
            Command(resume=resume_values), config=config, version="v2"
        )
        if completed.interrupts:
            raise RuntimeError("恢复后仍存在未处理的 interrupt")
        print_summary(completed.value["final_summary"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--thread-id",
        default="subgraph-interrupt-demo3",
        help="持久化会话 ID；进程重启后使用同一值继续",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(__file__).with_name("checkpoints.sqlite"),
        help="SQLite checkpoint 文件路径",
    )
    defaults = default_request()
    parser.add_argument("--topic", default=defaults.topic, help="发布主题")
    parser.add_argument("--content", default=defaults.content, help="待发布原始内容")
    parser.add_argument(
        "--channel", default=defaults.target_channel, help="目标发布渠道"
    )
    parser.add_argument(
        "--scheduled-at", default=defaults.scheduled_at, help="计划发布时间"
    )
    args = parser.parse_args()
    run_cli(
        args.thread_id,
        args.database,
        ReviewRequest(
            topic=args.topic,
            content=args.content,
            target_channel=args.channel,
            scheduled_at=args.scheduled_at,
        ),
    )


if __name__ == "__main__":
    main()
