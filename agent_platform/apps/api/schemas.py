"""Validated, deliberately bounded API requests."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


class PasswordChange(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class UserCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=512)
    role: Literal["admin", "member"] = "member"


class UserPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Literal["admin", "member"] | None = None
    active: bool | None = None


class ModelCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    alias: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$")
    active: bool = True
    input_price: str = "1"
    output_price: str = "3"
    context_window: int = Field(default=32768, ge=256, le=2000000)
    max_output_tokens: int = Field(default=2048, ge=1, le=128000)

    @field_validator("input_price", "output_price")
    @classmethod
    def price(cls, value: str) -> str:
        try:
            number = Decimal(value)
            if (
                not number.is_finite()
                or number <= 0
                or number > 1000000
                or number.as_tuple().exponent < -12
            ):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError("第一版价格必须为大于零、最多12位小数的金额") from None
        return str(number)


class ModelPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    alias: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$"
    )
    active: bool | None = None
    input_price: str | None = None
    output_price: str | None = None
    context_window: int | None = Field(default=None, ge=256, le=2000000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=128000)

    @field_validator("input_price", "output_price")
    @classmethod
    def price(cls, value: str | None) -> str | None:
        return ModelCreate.price(value) if value is not None else None


class AgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(
        default="你是一位专业、可靠的助手。", min_length=1, max_length=20000
    )
    model_id: str = Field(min_length=1, max_length=64)
    temperature: float = Field(default=0, ge=0, le=2)
    max_steps: int = Field(default=8, ge=1, le=30)
    max_tokens: int = Field(default=1024, ge=1, le=128000)
    tools: list[Literal["calculator", "current_time"]] = Field(
        default_factory=list, max_length=2
    )


class AgentPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_steps: int | None = Field(default=None, ge=1, le=30)
    max_tokens: int | None = Field(default=None, ge=1, le=128000)
    tools: list[Literal["calculator", "current_time"]] | None = Field(
        default=None, max_length=2
    )


class SessionCreate(StrictModel):
    agent_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=160)


class RunCreate(StrictModel):
    message: str = Field(min_length=1, max_length=32000)


class Credit(StrictModel):
    amount: str
    description: str = Field(min_length=1, max_length=500)


class Quota(StrictModel):
    scope: Literal["tenant", "user", "model"]
    subject_id: str = Field(min_length=1, max_length=64)
    rpm: int | None = Field(default=None, ge=0, le=1000000)
    tpm: int | None = Field(default=None, ge=0, le=1000000000)
    concurrent: int | None = Field(default=None, ge=0, le=10000)
    max_budget: str | None = None


class Reconcile(StrictModel):
    action: Literal["confirm", "write_off"]
    reason: str = Field(min_length=5, max_length=500)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Unblock(StrictModel):
    reason: str = Field(min_length=5, max_length=500)
