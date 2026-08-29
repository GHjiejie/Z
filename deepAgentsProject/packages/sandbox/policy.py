from __future__ import annotations

import re
import shlex
from fnmatch import fnmatch
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from packages.coding.errors import SandboxPolicyError


_DENIED_COMMAND_PATTERNS = [
    r"(^|[;&|]\s*)(sudo|su|mount|umount|nsenter|chroot|docker|podman|kubectl|helm|terraform|ansible-playbook)\b",
    r"/var/run/(docker|containerd)\.sock",
    r"(^|[\s/])git\s+(push|commit|send-pack)\b",
    r"(^|[\s/])(curl|wget|nc|ncat|ssh|scp|rsync)\b",
    r"rm\s+-[^\n]*(r|f)[^\n]*\s+/(\s|$)",
    r"/proc/(1|self)/(root|environ|mem)",
    r"(^|[/\s'\"])\.git([/\s'\"]|$)",
    r"(^|[/\s'\"])(skills|artifacts)([/\s'\"]|$)",
]

_MUTATING_GIT_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "bisect",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "fetch",
    "init",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "remote",
    "reset",
    "restore",
    "revert",
    "rm",
    "send-pack",
    "stash",
    "switch",
    "tag",
    "worktree",
}


@dataclass(frozen=True)
class SandboxPolicy:
    workspace_root: str
    protected_paths: tuple[str, ...]
    delivery_mode: str = "patch_only"
    approval_mode: str = "high_risk"

    @classmethod
    def from_plan(
        cls, coding_profile: dict, *, approval_mode: str = "high_risk"
    ) -> "SandboxPolicy":
        sandbox = coding_profile.get("sandbox", {})
        return cls(
            workspace_root=sandbox.get("workspace_root", "/workspace/repo"),
            protected_paths=tuple(coding_profile.get("protected_paths", [])),
            delivery_mode=coding_profile.get("delivery_mode", "patch_only"),
            approval_mode=approval_mode,
        )

    def authorize_path(self, path: str, operation: str) -> str:
        normalized = self.normalize_path(path)
        allowed_roots = (self.workspace_root, "/artifacts", "/skills")
        if not any(normalized == root or normalized.startswith(root + "/") for root in allowed_roots):
            raise SandboxPolicyError(f"Path is outside the governed workspace: {normalized}")
        relative = normalized.removeprefix(self.workspace_root).lstrip("/")
        if relative == ".git" or relative.startswith(".git/"):
            raise SandboxPolicyError("Direct access to Git metadata is denied")
        if operation in {"write", "edit", "delete"}:
            if normalized == "/skills" or normalized.startswith("/skills/"):
                raise SandboxPolicyError("Skills are immutable at runtime")
            if normalized == "/artifacts" or normalized.startswith("/artifacts/"):
                raise SandboxPolicyError("Artifact staging is platform-owned")
            if self.approval_mode == "never" and any(
                fnmatch(normalized, pattern) for pattern in self.protected_paths
            ):
                raise SandboxPolicyError(
                    "Protected path writes are denied when approvals are disabled"
                )
            # Protected source paths are gated by Deep Agents filesystem
            # permissions. Keeping that decision out of this final isolation
            # layer lets an explicitly approved graph interrupt resume exactly
            # once, while paths outside the sandbox remain unconditionally
            # denied here.
        return normalized

    def authorize_command(self, command: str) -> None:
        if not command.strip():
            raise SandboxPolicyError("Command cannot be blank")
        for pattern in _DENIED_COMMAND_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                raise SandboxPolicyError("Command is denied by the coding sandbox policy")
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            raise SandboxPolicyError("Command has invalid shell quoting") from exc
        lowered = [token.lower() for token in tokens]
        for index, token in enumerate(lowered):
            executable = token.rsplit("/", 1)[-1]
            if executable == "git" and any(
                candidate in _MUTATING_GIT_SUBCOMMANDS
                for candidate in lowered[index + 1 :]
            ):
                raise SandboxPolicyError(
                    "Mutating Git commands are disabled in patch_only mode"
                )
            if executable in {"gh", "glab", "hub"}:
                raise SandboxPolicyError("Git delivery CLIs are disabled in patch_only mode")
            remaining = lowered[index + 1 :]
            if (
                (executable in {"pip", "pip3"} and "install" in remaining)
                or (
                    executable in {"npm", "yarn", "pnpm"}
                    and any(action in remaining for action in {"add", "ci", "install", "update"})
                )
                or executable in {"apt", "apt-get", "apk", "brew", "npx"}
                or (
                    executable in {"python", "python3"}
                    and "-m" in remaining
                    and "pip" in remaining
                    and "install" in remaining
                )
            ):
                raise SandboxPolicyError(
                    "Runtime dependency installation is disabled by default"
                )

    @staticmethod
    def normalize_path(path: str) -> str:
        if any(ord(character) < 32 for character in path):
            raise SandboxPolicyError("Path must not contain control characters")
        candidate = PurePosixPath(path)
        if not candidate.is_absolute() or ".." in candidate.parts or "~" in candidate.parts:
            raise SandboxPolicyError("Path must be absolute and must not contain traversal")
        return str(candidate)
