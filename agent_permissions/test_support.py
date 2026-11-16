"""Explicit delegation setup for isolated API, Chat and browser test fixtures."""

from fastapi.testclient import TestClient


def grant_tools(
    client: TestClient, headers: dict, user_id: str, operations: list[str] | None = None
) -> dict:
    endpoint = f"/api/v1/users/{user_id}/agent-tools"
    response = client.get(endpoint, headers=headers)
    assert response.status_code == 200, response.text
    view = response.json()["data"]
    selected = (
        operations
        if operations is not None
        else [item["operation_id"] for item in view["items"] if item["available"]]
    )
    response = client.patch(
        endpoint,
        headers=headers,
        json={
            "expected_version": view["version"],
            "catalog_revision": view["catalog_revision"],
            "changes": [
                {"operation_id": operation, "enabled": True} for operation in selected
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]
