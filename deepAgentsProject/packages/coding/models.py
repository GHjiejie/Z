from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class RepositoryProvider(str, Enum):
    LOCAL_SNAPSHOT = "local_snapshot"
    GENERIC_GIT = "generic_git"
    GITHUB = "github"
    GITLAB = "gitlab"


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    provider: RepositoryProvider = RepositoryProvider.LOCAL_SNAPSHOT
    canonical_uri: str = Field(min_length=1, max_length=2048)
    default_branch: str = Field(default="main", min_length=1, max_length=255)
    credential_ref: Optional[str] = Field(default=None, max_length=255)
    access_policy_revision_id: str = "repository-project-default-v1"

    @field_validator("canonical_uri")
    @classmethod
    def canonical_uri_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("canonical_uri cannot be blank")
        return value.strip()


class RepositorySnapshotCreate(BaseModel):
    requested_ref: Optional[str] = Field(default=None, max_length=255)
    source_mode: Literal["committed_ref", "working_tree_snapshot"] = "committed_ref"


class WorkspaceBinding(BaseModel):
    repository_id: str = Field(min_length=1)
    base_ref: Optional[str] = Field(default=None, max_length=255)
    source_mode: Literal["committed_ref", "working_tree_snapshot"] = "committed_ref"


class VerificationPolicy(BaseModel):
    auto_discover: bool = True
    required_commands: List[str] = Field(default_factory=list, max_length=20)
    max_attempts: int = Field(default=2, ge=1, le=10)
    command_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    require_success: bool = True


class SandboxProfileSpec(BaseModel):
    revision_id: str = "sandbox-docker-v1"
    provider: Literal["docker", "kubernetes", "fake"] = "docker"
    image: str = "deepagent/coding-runtime:0.1.0"
    image_digest: str = "sha256:unresolved"
    user: str = "10001:10001"
    cpu_limit: float = Field(default=2.0, gt=0, le=32)
    memory_mb: int = Field(default=4096, ge=128, le=131072)
    disk_mb: int = Field(default=10240, ge=128, le=1048576)
    pids_limit: int = Field(default=256, ge=16, le=32768)
    command_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    run_timeout_seconds: int = Field(default=1800, ge=10, le=86400)
    max_output_bytes: int = Field(default=200_000, ge=1024, le=10_000_000)
    network_mode: Literal["deny_by_default", "allowlist"] = "deny_by_default"
    workspace_root: str = "/workspace/repo"
    read_only_rootfs: bool = True
    lifecycle: Literal["run_scoped", "thread_scoped", "agent_scoped"] = "thread_scoped"
    ttl_seconds: int = Field(default=86400, ge=60, le=2_592_000)

    @field_validator("workspace_root")
    @classmethod
    def absolute_workspace_root(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError("workspace_root must be an absolute normalized path")
        return value.rstrip("/") or "/"


class CodingProfileSpec(BaseModel):
    enabled: bool = False
    sandbox: SandboxProfileSpec = Field(default_factory=SandboxProfileSpec)
    repository_policy_revision_id: str = "repository-project-default-v1"
    delivery_mode: Literal["patch_only", "commit", "pull_request"] = "patch_only"
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    protected_paths: List[str] = Field(
        default_factory=lambda: [
            "/workspace/repo/.env*",
            "/workspace/repo/.github/workflows/**",
            "/workspace/repo/**/secrets/**",
        ]
    )
    max_changed_files: int = Field(default=50, ge=1, le=10_000)
    max_diff_lines: int = Field(default=5000, ge=1, le=1_000_000)


class ChangeSetDecision(BaseModel):
    message: Optional[str] = Field(default=None, max_length=2000)
