from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationDecision(EvaluationModel):
    type: Literal["approve", "edit", "reject", "respond"]
    edited_arguments: dict | None = None
    message: str | None = Field(default=None, max_length=10000)


class EvaluationCase(EvaluationModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    category: Literal["functional", "safety", "recovery", "cost", "knowledge", "coding"]
    input: str = Field(min_length=1, max_length=100_000)
    expected_status: Literal[
        "SUCCEEDED", "FAILED", "FAILED_BUDGET", "TIMED_OUT", "CANCELLED", "WAITING_FOR_APPROVAL",
    ] = "SUCCEEDED"
    output_contains: list[str] = Field(default_factory=list, max_length=20)
    output_not_contains: list[str] = Field(default_factory=list, max_length=20)
    required_event_types: list[str] = Field(default_factory=list, max_length=20)
    expected_decisions: list[EvaluationDecision] = Field(default_factory=list, max_length=50)
    required_document_ids: list[str] = Field(default_factory=list, max_length=20)
    expected_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_cost: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    max_duration_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def meaningful_assertions(self):
        lists = (self.output_contains, self.output_not_contains, self.required_event_types, self.required_document_ids)
        if any(not item.strip() or len(item) > 4000 for values in lists for item in values):
            raise ValueError("Evaluation assertions must be nonblank and at most 4000 characters")
        if self.category == "functional" and not self.output_contains:
            raise ValueError("Functional cases require a positive output assertion")
        if self.category == "cost" and self.max_cost is None:
            raise ValueError("Cost cases require an explicit cost ceiling")
        if self.category == "knowledge" and not self.required_document_ids:
            raise ValueError("Knowledge cases require expected source documents")
        if self.category == "coding" and not self.expected_source_sha256:
            raise ValueError("Coding cases require a pinned source archive hash")
        return self


class EvaluationSuiteCreate(EvaluationModel):
    name: str = Field(min_length=2, max_length=100)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Evaluation case IDs must be unique")
        return self


class EvaluationPolicyUpdate(EvaluationModel):
    suite_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=0)
    max_age_seconds: int = Field(default=86400, ge=60, le=604800)
    reason: str = Field(min_length=5, max_length=500)


class EvaluationRequest(EvaluationModel):
    suite_id: str = Field(min_length=1, max_length=100)
    case_runs: dict[str, str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def separate_executions(self):
        if len(set(self.case_runs.values())) != len(self.case_runs):
            raise ValueError("Each evaluation case requires its own Run")
        return self
