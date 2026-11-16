"""Offline checks for project-local credentials and Responses API isolation."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_permissions.api import create_app
from agent_permissions.chat_model import create_chat_model
from agent_permissions.config import Settings


class ProjectChatModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_file = self.directory / ".env"
        self.env_file.write_text(
            "AGENT_USERS_LLM_BASE_URL=https://project.example/v1\n"
            "AGENT_USERS_LLM_API_KEY=project-test-key\n"
            "AGENT_USERS_LLM_MODEL=project-model\n"
        )

    def test_local_file_ignores_shared_settings_and_does_not_export_variables(
        self,
    ) -> None:
        shared = {
            "OPENAI_BASE_URL": "https://shared.example/v1",
            "OPENAI_API_KEY": "shared-test-key",
            "MODEL": "shared-model",
        }
        with patch.dict(os.environ, shared, clear=True):
            before = dict(os.environ)
            model = create_chat_model(env_file=self.env_file)
            self.assertEqual(model.model_name, "project-model")
            self.assertEqual(model.openai_api_base, "https://project.example/v1")
            self.assertEqual(
                model.openai_api_key.get_secret_value(), "project-test-key"
            )
            self.assertTrue(model.use_responses_api)
            self.assertEqual(model.reasoning, {"effort": "medium", "summary": "auto"})
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("project-test-key", repr(model.openai_api_key))

    def test_only_namespaced_process_variables_override_the_project_file(self) -> None:
        with patch.dict(
            os.environ, {"AGENT_USERS_LLM_MODEL": "process-model"}, clear=True
        ):
            model = create_chat_model(env_file=self.env_file)
            self.assertEqual(model.model_name, "process-model")
            self.assertEqual(
                model.openai_api_key.get_secret_value(), "project-test-key"
            )

    def test_missing_project_configuration_never_falls_back_to_shared_credentials(
        self,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "shared-test-key",
                    "OPENAI_BASE_URL": "https://shared.example/v1",
                    "MODEL": "shared-model",
                },
                clear=True,
            ),
            patch(
                "agent_permissions.chat_model.DEFAULT_ENV_FILE",
                self.directory / "missing.env",
            ),
            self.assertRaisesRegex(ValueError, "AGENT_USERS_LLM_"),
        ):
            create_chat_model()

    def test_api_startup_does_not_require_model_credentials(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "agent_permissions.chat_model.DEFAULT_ENV_FILE",
                self.directory / "missing.env",
            ),
            TestClient(
                create_app(Settings(database_path=self.directory / "users.sqlite3"))
            ) as client,
        ):
            response = client.get("/agent-tools")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["tools"]), 1)


if __name__ == "__main__":
    unittest.main()
