"""Immutable snapshots with validated activation and explicit stale-call rejection."""

import json
from threading import RLock

from api_tools.compiler import ContractError, canonical, verify_bundle
from api_tools.openapi import OpenAPIRequests


class ToolRegistry:
    def __init__(
        self,
        bundle: dict,
        *,
        expected: dict | None = None,
        requests: OpenAPIRequests | None = None,
    ) -> None:
        self._lock = RLock()
        self._snapshot = ""
        self.activate(
            bundle, expected=bundle if expected is None else expected, requests=requests
        )

    def activate(
        self, bundle: dict, *, expected: dict, requests: OpenAPIRequests | None = None
    ) -> None:
        """The application supplies a freshly compiled contract for this API build."""
        verify_bundle(bundle)
        snapshot = canonical(bundle)
        if snapshot != canonical(expected):
            raise ContractError("Tool bundle does not match this API build")
        mappings = (
            requests if requests is not None else OpenAPIRequests.from_manifest(bundle)
        )
        with self._lock:
            self._snapshot = snapshot
            self._requests = mappings

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(self._snapshot)

    def execution_snapshot(self) -> tuple[dict, OpenAPIRequests]:
        """A call sees one revision, even if a new contract is activated concurrently."""
        with self._lock:
            return json.loads(self._snapshot), self._requests


def permitted(policy: dict, permissions: set[str], *, authenticated: bool) -> bool:
    if policy["authentication"] != "none" and not authenticated:
        return False
    return set(policy["allPermissions"]) <= permissions and (
        not policy["anyPermissions"]
        or bool(set(policy["anyPermissions"]) & permissions)
    )
