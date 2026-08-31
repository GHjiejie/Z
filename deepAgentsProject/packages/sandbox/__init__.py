from __future__ import annotations

from typing import Any

__all__ = [
    "DockerSandboxProvider",
    "FakeSandboxProvider",
    "RemoteSandboxProvider",
    "SandboxManager",
]


def __getattr__(name: str) -> Any:
    if name == "DockerSandboxProvider":
        from packages.sandbox.docker_provider import DockerSandboxProvider

        return DockerSandboxProvider
    if name == "FakeSandboxProvider":
        from packages.sandbox.fake_provider import FakeSandboxProvider

        return FakeSandboxProvider
    if name == "RemoteSandboxProvider":
        from packages.sandbox.remote_provider import RemoteSandboxProvider

        return RemoteSandboxProvider
    if name == "SandboxManager":
        from packages.sandbox.manager import SandboxManager

        return SandboxManager
    raise AttributeError(name)
