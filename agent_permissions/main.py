"""Run with: uv run uvicorn agent_permissions.main:app --port 8000."""

from agent_permissions.api import create_app

app = create_app()
