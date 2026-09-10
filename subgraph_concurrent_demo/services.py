"""Deterministic async services used to make concurrency visible offline."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from subgraph_concurrent_demo.models import BranchName, ServiceOption, TravelRequest


class ServiceUnavailable(RuntimeError):
    """Raised by a simulated dependency so a branch can degrade gracefully."""


@dataclass(frozen=True)
class SimulatedTravelServices:
    """Async stand-ins for three independent remote APIs.

    Configuration is immutable. Per-run data stays in LangGraph state, so one
    compiled graph can safely serve multiple invocations.
    """

    delays: Mapping[BranchName, float] = field(
        default_factory=lambda: {
            "transport": 0.45,
            "hotel": 0.70,
            "activity": 0.25,
        }
    )
    fail_branches: frozenset[BranchName] = field(default_factory=frozenset)

    async def _before_result(self, branch: BranchName) -> None:
        await asyncio.sleep(self.delays.get(branch, 0.0))
        if branch in self.fail_branches:
            raise ServiceUnavailable(f"{branch} service is unavailable")

    async def search_transport(self, request: TravelRequest) -> list[ServiceOption]:
        await self._before_result("transport")
        base = 180 * request.travelers
        return [
            ServiceOption(
                title=f"{request.origin}至{request.destination}高铁往返",
                estimated_cost=base * 2,
                score=90,
                details=["市区到市区", "建议提前预约相邻座位"],
            ),
            ServiceOption(
                title=f"{request.origin}至{request.destination}大巴往返",
                estimated_cost=int(base * 1.35),
                score=72,
                details=["价格较低", "行程时间较长"],
            ),
        ]

    async def search_hotels(self, request: TravelRequest) -> list[ServiceOption]:
        await self._before_result("hotel")
        nights = max(1, request.days - 1)
        return [
            ServiceOption(
                title=f"{request.destination}市中心舒适酒店",
                estimated_cost=420 * nights,
                score=86,
                details=[f"入住 {nights} 晚", "公共交通便利"],
            ),
            ServiceOption(
                title=f"{request.destination}经济型酒店",
                estimated_cost=260 * nights,
                score=76,
                details=[f"入住 {nights} 晚", "距离市中心稍远"],
            ),
        ]

    async def search_activities(self, request: TravelRequest) -> list[ServiceOption]:
        await self._before_result("activity")
        interests = "、".join(request.interests) or "城市漫步"
        return [
            ServiceOption(
                title=f"{request.destination}{interests}体验",
                estimated_cost=120 * request.travelers,
                score=88,
                details=[f"覆盖兴趣：{interests}", "预留半天自由活动"],
            ),
            ServiceOption(
                title=f"{request.destination}经典景点组合",
                estimated_cost=80 * request.travelers,
                score=78,
                details=["适合首次到访", "按天气调整顺序"],
            ),
        ]
