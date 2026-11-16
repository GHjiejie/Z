"""Immutable snapshots with validated activation and explicit stale-call rejection."""

import json
from threading import RLock

from api_tools.compiler import ContractError, canonical, verify_bundle


class ToolRegistry:
    def __init__(self, bundle: dict) -> None:
        self._lock = RLock()
        self._snapshot = ""
        self.activate(bundle, expected=bundle)

    def activate(self, bundle: dict, *, expected: dict) -> None:
        """The application supplies a freshly compiled contract for this API build."""
        verify_bundle(bundle)
        if canonical(bundle) != canonical(expected):
            raise ContractError("Tool bundle does not match this API build")
        with self._lock:
            self._snapshot = canonical(bundle)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(self._snapshot)


def permitted(policy: dict, permissions: set[str], *, authenticated: bool) -> bool:
    if policy["authentication"] != "none" and not authenticated:
        return False
    return set(policy["allPermissions"]) <= permissions and (
        not policy["anyPermissions"]
        or bool(set(policy["anyPermissions"]) & permissions)
    )
