"""Offline checks for input selection, model configuration and report isolation."""

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from reports import MAX_INPUT_BYTES, create_run, read_requirement, validate_text

from main import resolve_input


class InputTests(unittest.TestCase):
    def test_utf8_bom_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "需求.md"
            path.write_bytes(b"\xef\xbb\xbf" + "任务管理需求".encode())
            self.assertEqual(read_requirement(path), "任务管理需求")
            path.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                read_requirement(path)
            path.write_bytes(b"a" * (MAX_INPUT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                read_requirement(path)
            with self.assertRaisesRegex(ValueError, "仅支持"):
                read_requirement(Path(folder) / "document.pdf")

    def test_empty_and_oversized_text(self) -> None:
        for text in ("", " \n\t "):
            with self.assertRaisesRegex(ValueError, "不能为空"):
                validate_text(text)
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            validate_text("需" * MAX_INPUT_BYTES)

    def test_cli_input_overrides_environment_and_env_sources_conflict(self) -> None:
        args = argparse.Namespace(task="明确输入", input_file=None, mode="requirements")
        with patch.dict(os.environ, {"TASK": "环境任务", "INPUT": "missing.md"}):
            self.assertEqual(resolve_input(args), ("明确输入", "直接输入"))
            args.task = None
            with self.assertRaisesRegex(ValueError, "只能指定一个"):
                resolve_input(args)

    def test_default_requirements_input_is_the_example(self) -> None:
        args = argparse.Namespace(task=None, input_file=None, mode="requirements")
        with patch.dict(os.environ, {}, clear=True):
            text, source = resolve_input(args)
        self.assertIn("团队任务管理", text)
        self.assertTrue(source.endswith("examples/task_manager.md"))

    def test_only_local_config_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = root / "child"
            child.mkdir()
            (root / ".env").write_text("OPENAI_API_KEY=parent\nMODEL=parent\n")
            with patch.object(config, "PROJECT_DIR", child):
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(ValueError, "缺少配置"):
                        config.load_llm_config()
                    (child / ".env").write_text(
                        "OPENAI_API_KEY=local-${MISSING}\nMODEL=local-model\n"
                    )
                    loaded = config.load_llm_config()
                    self.assertEqual(loaded["api_key"], "local-${MISSING}")
                with patch.dict(os.environ, {"MODEL": "override"}, clear=True):
                    self.assertEqual(config.load_llm_config()["model"], "override")
                with (
                    patch.dict(os.environ, {"OPENAI_API": "invalid"}, clear=True),
                    self.assertRaisesRegex(ValueError, "OPENAI_API"),
                ):
                    config.load_llm_config()

    def test_repeated_runs_never_overwrite_previous_input(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = create_run(Path(folder), "first")
            second = create_run(Path(folder), "second")
            self.assertNotEqual(first, second)
            self.assertEqual((first / "00-input.md").read_text(), "first\n")
            self.assertEqual((second / "00-input.md").read_text(), "second\n")

    def test_local_proxy_bypass_respects_process_settings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text(
                "OPENAI_API_KEY=local-key\nMODEL=local-model\n"
                "no_proxy=gateway.example.com\n"
            )
            with patch.object(config, "PROJECT_DIR", root):
                with patch.dict(os.environ, {}, clear=True):
                    config.load_llm_config()
                    self.assertEqual(os.environ["no_proxy"], "gateway.example.com")
                for variable in ("no_proxy", "NO_PROXY"):
                    with patch.dict(
                        os.environ, {variable: "explicit.example"}, clear=True
                    ):
                        config.load_llm_config()
                        self.assertEqual(os.environ[variable], "explicit.example")
                        other = "NO_PROXY" if variable == "no_proxy" else "no_proxy"
                        self.assertNotIn(other, os.environ)


if __name__ == "__main__":
    unittest.main()
