"""Offline behavioral tests for parallel subgraph interrupts and resumption."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from subgraph_interrupt_demo.main import (
    BRANCH_ORDER,
    build_graph,
    checkpoint_serializer,
    default_request,
)
from subgraph_interrupt_demo.models import BranchName, ReviewProposal, ReviewRequest


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def decisions_by_branch(output, decisions: dict[BranchName, dict]) -> dict[str, dict]:
    return {
        item.id: decisions[item.value["branch"]] for item in reversed(output.interrupts)
    }


def deterministic_proposal_builder(
    branch: BranchName, request: ReviewRequest
) -> ReviewProposal:
    """Keep unit tests offline; the default application path calls the real model."""

    return ReviewProposal(
        branch=branch,
        title=f"{branch} review",
        proposed_value=f"{request.topic}:{branch}",
        notes=["offline test proposal"],
    )


class BarrierProposalBuilder:
    """No branch can finish unless all three proposal calls overlap."""

    def __init__(self) -> None:
        self.barrier = threading.Barrier(3)
        self.started: list[BranchName] = []

    def __call__(self, branch: BranchName, request: ReviewRequest) -> ReviewProposal:
        self.started.append(branch)
        self.barrier.wait(timeout=0.5)
        return ReviewProposal(
            branch=branch,
            title=f"{branch} review",
            proposed_value=f"{request.topic}:{branch}",
        )


class SubgraphInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_subgraphs_overlap_and_emit_unique_interrupts(self) -> None:
        proposal_builder = BarrierProposalBuilder()
        graph = build_graph(
            InMemorySaver(serde=checkpoint_serializer()), proposal_builder
        )
        output = await asyncio.wait_for(
            graph.ainvoke(
                {"request": default_request()},
                config=config("parallel"),
                version="v2",
            ),
            timeout=1.0,
        )

        self.assertCountEqual(proposal_builder.started, BRANCH_ORDER)
        self.assertEqual(len(output.interrupts), 3)
        self.assertEqual(len({item.id for item in output.interrupts}), 3)
        self.assertEqual(
            {item.value["branch"] for item in output.interrupts},
            set(BRANCH_ORDER),
        )
        self.assertNotIn("final_summary", output.value)

    async def test_resume_map_matches_decisions_by_id_not_input_order(self) -> None:
        graph = build_graph(
            InMemorySaver(serde=checkpoint_serializer()),
            deterministic_proposal_builder,
        )
        cfg = config("resume-map")
        paused = await graph.ainvoke(
            {"request": default_request()}, config=cfg, version="v2"
        )
        resume_values = decisions_by_branch(
            paused,
            {
                "content": {
                    "type": "edit",
                    "replacement": "人工修改后的公告内容",
                    "reason": "表达更清晰",
                },
                "compliance": {"type": "approve", "reason": "合规通过"},
                "delivery": {"type": "reject", "reason": "发布时间不合适"},
            },
        )
        completed = await graph.ainvoke(
            Command(resume=resume_values), config=cfg, version="v2"
        )

        self.assertFalse(completed.interrupts)
        summary = completed.value["final_summary"]
        self.assertEqual([item.branch for item in summary.results], list(BRANCH_ORDER))
        self.assertEqual(
            [item.status for item in summary.results],
            ["edited", "approved", "rejected"],
        )
        self.assertEqual(summary.results[0].final_value, "人工修改后的公告内容")
        self.assertFalse(summary.ready_to_publish)

    async def test_join_runs_once_after_all_approvals_and_private_state_is_hidden(
        self,
    ) -> None:
        graph = build_graph(
            InMemorySaver(serde=checkpoint_serializer()),
            deterministic_proposal_builder,
        )
        cfg = config("join")
        paused = await graph.ainvoke(
            {"request": default_request()}, config=cfg, version="v2"
        )
        completed = await graph.ainvoke(
            Command(
                resume={item.id: {"type": "approve"} for item in paused.interrupts}
            ),
            config=cfg,
            version="v2",
        )
        state = completed.value

        self.assertTrue(state["final_summary"].ready_to_publish)
        self.assertEqual(len(state["review_results"]), 3)
        self.assertEqual(
            sum(event.stage == "summarized" for event in state["audit_events"]),
            1,
        )
        self.assertNotIn("proposal", state)
        self.assertNotIn("decision", state)

    async def test_sqlite_interrupts_survive_graph_and_connection_restart(self) -> None:
        cfg = config("restart")
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "checkpoints.sqlite"
            with closing(
                sqlite3.connect(database, check_same_thread=False)
            ) as first_connection:
                first_graph = build_graph(
                    SqliteSaver(first_connection, serde=checkpoint_serializer()),
                    deterministic_proposal_builder,
                )
                first = first_graph.invoke(
                    {"request": default_request()}, config=cfg, version="v2"
                )

            with closing(
                sqlite3.connect(database, check_same_thread=False)
            ) as second_connection:
                restored_graph = build_graph(
                    SqliteSaver(second_connection, serde=checkpoint_serializer()),
                    deterministic_proposal_builder,
                )
                rediscovered = restored_graph.invoke(None, config=cfg, version="v2")
                self.assertEqual(
                    {item.id for item in first.interrupts},
                    {item.id for item in rediscovered.interrupts},
                )
                completed = restored_graph.invoke(
                    Command(
                        resume={
                            item.id: {"type": "approve"}
                            for item in rediscovered.interrupts
                        }
                    ),
                    config=cfg,
                    version="v2",
                )
                self.assertTrue(completed.value["final_summary"].ready_to_publish)


if __name__ == "__main__":
    unittest.main()
