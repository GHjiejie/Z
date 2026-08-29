from __future__ import annotations

from typing import Any

__all__ = ["DockerSandboxProvider", "FakeSandboxProvider", "SandboxManager"]


def __getattr__(name: str) -> Any:
    if name == "DockerSandboxProvider":
        from packages.sandbox.docker_provider import DockerSandboxProvider

        return DockerSandboxProvider
    if name == "FakeSandboxProvider":
        from packages.sandbox.fake_provider import FakeSandboxProvider

        return FakeSandboxProvider
    if name == "SandboxManager":
        from packages.sandbox.manager import SandboxManager

        return SandboxManager
    raise AttributeError(name)
