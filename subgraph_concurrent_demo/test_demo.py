"""Behavioral tests for concurrency, reducers, isolation, and degradation."""

from __future__ import annotations

import asyncio
import unittest

from subgraph_concurrent_demo.main import build_graph, default_request
from subgraph_concurrent_demo.models import BranchName, ServiceOption, TravelRequest
from subgraph_concurrent_demo.services import SimulatedTravelServices

ZERO_DELAYS = {"transport": 0.0, "hotel": 0.0, "activity": 0.0}


async def deterministic_ranker(
    branch: BranchName,
    request: TravelRequest,
    candidates: list[ServiceOption],
) -> ServiceOption:
    """Keep unit tests offline; the demo's default ranker calls the real model."""

    del branch, request
    return max(
        candidates,
        key=lambda option: (option.score, -option.estimated_cost, option.title),
    )


class BarrierServices(SimulatedTravelServices):
    """A test double that can finish only if all three calls overlap."""

    def __init__(self) -> None:
        super().__init__(delays=ZERO_DELAYS)
        object.__setattr__(self, "barrier", asyncio.Barrier(3))
        object.__setattr__(self, "started", [])

    async def _meet(self, branch: str) -> None:
        self.started.append(branch)
        await self.barrier.wait()

    async def search_transport(self, request: TravelRequest) -> list[ServiceOption]:
        await self._meet("transport")
        return await super().search_transport(request)

    async def search_hotels(self, request: TravelRequest) -> list[ServiceOption]:
        await self._meet("hotel")
        return await super().search_hotels(request)

    async def search_activities(self, request: TravelRequest) -> list[ServiceOption]:
        await self._meet("activity")
        return await super().search_activities(request)


class ConcurrentSubgraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_subgraphs_really_overlap(self) -> None:
        services = BarrierServices()
        result = await asyncio.wait_for(
            build_graph(services, ranker=deterministic_ranker).ainvoke(
                {"request": default_request()}
            ),
            timeout=1.0,
        )

        self.assertCountEqual(
            services.started,
            ["transport", "hotel", "activity"],
        )
        self.assertEqual(result["final_plan"].status, "complete")

    async def test_reducers_merge_all_outputs_and_private_state_does_not_leak(
        self,
    ) -> None:
        services = SimulatedTravelServices(delays=ZERO_DELAYS)
        result = await build_graph(services, ranker=deterministic_ranker).ainvoke(
            {"request": default_request()}
        )

        self.assertEqual(
            {proposal.branch for proposal in result["proposals"]},
            {"transport", "hotel", "activity"},
        )
        self.assertNotIn("candidates", result)
        self.assertNotIn("selected", result)
        self.assertNotIn("branch_error", result)
        started = {
            event.branch
            for event in result["audit_events"]
            if event.stage == "search" and event.status == "started"
        }
        self.assertEqual(started, {"transport", "hotel", "activity"})

    async def test_join_is_deterministic_and_preserves_branch_order(self) -> None:
        services = SimulatedTravelServices(
            delays={"transport": 0.03, "hotel": 0.01, "activity": 0.02}
        )
        result = await build_graph(services, ranker=deterministic_ranker).ainvoke(
            {"request": default_request()}
        )

        self.assertEqual(
            [proposal.branch for proposal in result["final_plan"].proposals],
            ["transport", "hotel", "activity"],
        )
        self.assertEqual(result["final_plan"].status, "complete")

    async def test_one_failed_branch_degrades_without_stopping_siblings(self) -> None:
        services = SimulatedTravelServices(
            delays=ZERO_DELAYS,
            fail_branches=frozenset({"hotel"}),
        )
        result = await build_graph(services, ranker=deterministic_ranker).ainvoke(
            {"request": default_request()}
        )
        plan = result["final_plan"]

        self.assertEqual(plan.status, "partial")
        hotel = next(item for item in plan.proposals if item.branch == "hotel")
        self.assertEqual(hotel.status, "degraded")
        self.assertTrue(any("住宿分支已降级" in item for item in plan.warnings))
        self.assertEqual(len(plan.proposals), 3)


if __name__ == "__main__":
    unittest.main()
