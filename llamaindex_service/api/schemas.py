"""Public inputs deliberately exclude identity and provider configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:@-]+$")
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KnowledgeBaseCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class KnowledgeBaseUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["active", "disabled"] | None = None


class KnowledgePermissions(Input):
    members: dict[Identifier, Literal["owner", "editor", "reader"]]


class DocumentPermissions(Input):
    allowed_users: list[Identifier] = Field(default_factory=list, max_length=200)
    allowed_roles: list[Identifier] = Field(default_factory=list, max_length=100)


class SearchRequest(Input):
    knowledge_base_ids: list[Identifier] = Field(min_length=1, max_length=20)
    document_ids: list[Identifier] | None = Field(default=None, max_length=100)
    query: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=8, ge=1, le=20)
    filters: dict[Literal["filename", "content_type"], str] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def bounded_filters(self) -> "SearchRequest":
        if any(len(v) > 255 for v in self.filters.values()):
            raise ValueError("Filter values must not exceed 255 characters")
        return self


class AnswerRequest(SearchRequest):
    conversation_id: Identifier | None = None
    stream: bool = False


class ConversationCreate(Input):
    knowledge_base_ids: list[Identifier] = Field(min_length=1, max_length=20)
    title: str = Field(default="新会话", min_length=1, max_length=200)


class FeedbackCreate(Input):
    rating: Literal["positive", "negative"]
    category: Literal["retrieval", "answer", "citation", "other"] = "other"
    comment: str = Field(default="", max_length=2000)


class GenerationCreate(Input):
    embedding_model: str = Field(min_length=1, max_length=255)
    embedding_revision: str = Field(min_length=1, max_length=128)
    embedding_dimension: int = Field(ge=8, le=2000)
