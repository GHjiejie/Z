"""HTTP entry point.

For Docker PostgreSQL, pass --env-file agent_permissions/deploy/.env to uvicorn.
See docs/docker-database.md for migration and launch commands.
"""

from agent_permissions.api import create_app

app = create_app()
