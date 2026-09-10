"""Typed state and domain models for the concurrent subgraph demo."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

BranchName = Literal["transport", "hotel", "activity"]
ProposalStatus = Literal["ok", "degraded"]
PlanStatus = Literal["complete", "partial", "failed"]


class TravelRequest(BaseModel):
    """The immutable business context shared by every expert subgraph."""

    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    days: int = Field(ge=1)
    travelers: int = Field(ge=1)
    budget: int = Field(gt=0, description="Total budget in CNY")
    interests: list[str] = Field(default_factory=list)


class ServiceOption(BaseModel):
    """A private candidate returned by one simulated external service."""

    title: str
    estimated_cost: int = Field(ge=0)
    score: int = Field(ge=0, le=100)
    details: list[str] = Field(default_factory=list)


class CandidateSelection(BaseModel):
    """A model-selected zero-based index plus a concise explanation."""

    selected_index: int = Field(
        ge=0,
        description="被选候选项在输入 candidates 数组中的下标，从 0 开始",
    )
    rationale: str = Field(
        min_length=1,
        description="结合旅行需求、费用和评分给出的简短中文选择理由",
    )


class Proposal(BaseModel):
    """The normalized contract published by every parallel expert."""

    branch: BranchName
    status: ProposalStatus
    title: str
    estimated_cost: int = Field(ge=0)
    score: int = Field(ge=0, le=100)
    details: list[str] = Field(default_factory=list)


class AuditEvent(BaseModel):
    """A reducer-friendly lifecycle record stored in graph state."""

    branch: str
    stage: str
    status: Literal["started", "completed", "failed"]
    message: str


class TravelPlan(BaseModel):
    status: PlanStatus
    proposals: list[Proposal]
    total_cost: int = Field(ge=0)
    remaining_budget: int
    warnings: list[str]
    summary: str


class TripInput(TypedDict):
    request: TravelRequest


class TripState(TripInput, total=False):
    """Parent state. Parallel writes are legal only on reducer-backed keys."""

    proposals: Annotated[list[Proposal], operator.add]
    warnings: Annotated[list[str], operator.add]
    audit_events: Annotated[list[AuditEvent], operator.add]
    final_plan: TravelPlan
    final_answer: str


class IntakeInput(TypedDict):
    request: TravelRequest


class IntakeOutput(TypedDict, total=False):
    request: TravelRequest
    audit_events: Annotated[list[AuditEvent], operator.add]


class IntakeState(IntakeInput, IntakeOutput, total=False):
    normalized_request: TravelRequest


class ExpertInput(TypedDict):
    request: TravelRequest


class ExpertOutput(TypedDict, total=False):
    proposals: Annotated[list[Proposal], operator.add]
    warnings: Annotated[list[str], operator.add]
    audit_events: Annotated[list[AuditEvent], operator.add]


class ExpertState(ExpertInput, ExpertOutput, total=False):
    """Private fields exist only while one expert subgraph is running."""

    candidates: list[ServiceOption]
    selected: ServiceOption
    branch_error: str


class SynthesisInput(TypedDict):
    request: TravelRequest
    proposals: list[Proposal]
    warnings: list[str]


class SynthesisOutput(TypedDict, total=False):
    final_plan: TravelPlan
    audit_events: Annotated[list[AuditEvent], operator.add]


class SynthesisState(SynthesisInput, SynthesisOutput, total=False):
    ordered_proposals: list[Proposal]
    derived_warnings: list[str]
