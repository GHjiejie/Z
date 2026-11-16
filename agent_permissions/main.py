"""HTTP entry point.

For Docker PostgreSQL, pass --env-file agent_permissions/deploy/.env to uvicorn.
See docs/docker-database.md for migration and launch commands.
"""

import os
from pathlib import Path

from agent_permissions.api import create_app

manifest = os.getenv("AGENT_USERS_TOOLS_MANIFEST")
app = create_app(tools_manifest=Path(manifest) if manifest else None)
