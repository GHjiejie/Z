"""Exercise real CrewAI orchestration through a local OpenAI-compatible fixture."""

import json
import os
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

PROJECT = Path(__file__).resolve().parents[1]
ANSWERS = (
    "# 需求分析\n\nFR-01 创建任务。AC-01 标题不能为空。ANALYSIS_EVIDENCE",
    "# 技术方案\n\n任务模块覆盖 FR-01。DESIGN_EVIDENCE",
    "# 方案评审\n\nP1：权限规则待确认。开发清单：确认后实现 FR-01。REVIEW_EVIDENCE",
)


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.requests: list[dict] = []
        self.fail_after: int | None = None
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.requests.append({"path": self.path, "body": body})
                index = len(case.requests) - 1
                if case.fail_after is not None and index >= case.fail_after:
                    status = 400
                    payload = {
                        "error": {
                            "message": "Fixture failure",
                            "type": "invalid_request_error",
                        }
                    }
                else:
                    status = 200
                    payload = {
                        "id": f"chatcmpl-local-{index}",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "Final Answer: " + ANSWERS[index % 3],
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                        },
                    }
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = dict(os.environ)
        for name in ("TASK", "INPUT", "OUTPUT_DIR"):
            self.env.pop(name, None)
        self.env.update(
            OPENAI_API_KEY="local-fixture-key",
            MODEL="local/test-model",
            OPENAI_BASE_URL=f"http://127.0.0.1:{self.server.server_port}/v1",
            OPENAI_API="completions",
            CREWAI_DISABLE_TELEMETRY="true",
            OTEL_SDK_DISABLED="true",
        )

    def run_make(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["make", *arguments, f"OUTPUT_DIR={self.folder / 'reports'}"],
            cwd=PROJECT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def test_three_roles_receive_prior_results_and_save_report(self) -> None:
        source = self.folder / "需求 $100 $(name).md"
        source.write_text(
            "创建团队任务管理系统；未知权限规则请标为待确认。", encoding="utf-8"
        )
        result = self.run_make("analyze", f"INPUT={source}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.requests), 3)
        bodies = [item["body"] for item in self.requests]
        self.assertTrue(
            all(item["path"] == "/v1/chat/completions" for item in self.requests)
        )
        self.assertIn("ANALYSIS_EVIDENCE", json.dumps(bodies[1]))
        self.assertIn("ANALYSIS_EVIDENCE", json.dumps(bodies[2]))
        self.assertIn("DESIGN_EVIDENCE", json.dumps(bodies[2]))
        self.assertEqual(bodies[0]["model"], "local/test-model")
        run_dirs = list((self.folder / "reports").iterdir())
        self.assertEqual(len(run_dirs), 1)
        output = run_dirs[0]
        self.assertEqual(
            {item.name for item in output.iterdir()},
            {
                "00-input.md",
                "01-requirements.md",
                "02-design.md",
                "03-review.md",
                "report.md",
            },
        )
        report = (output / "report.md").read_text()
        for marker in ("ANALYSIS_EVIDENCE", "DESIGN_EVIDENCE", "REVIEW_EVIDENCE"):
            self.assertIn(marker, report)
        self.assertNotIn(self.env["OPENAI_API_KEY"], report)
        self.assertEqual(
            (output / "00-input.md").read_text().strip(), source.read_text()
        )

    def test_check_has_no_model_calls_or_reports(self) -> None:
        result = self.run_make("check-analyze")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 个 Agent", result.stdout)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.folder / "reports").exists())

    def test_midrun_failure_keeps_analysis_without_final_report(self) -> None:
        self.fail_after = 1
        result = self.run_make("analyze", "TASK=开发任务管理工具")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("没有生成完整报告", result.stderr)
        run_dir = next((self.folder / "reports").iterdir())
        self.assertTrue((run_dir / "01-requirements.md").exists())
        self.assertFalse((run_dir / "report.md").exists())
        self.assertIn("ANALYSIS_EVIDENCE", (run_dir / "01-requirements.md").read_text())

    def test_original_run_preserves_literal_task_and_single_agent(self) -> None:
        task = '请保留费用 $100、$(name)、$(shell printf unexpected)、"引号" 和分号 ;'
        result = self.run_make("run", f"TASK={task}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.requests), 1)
        messages = self.requests[0]["body"]["messages"]
        self.assertTrue(any(task in item.get("content", "") for item in messages))
        self.assertIn("ANALYSIS_EVIDENCE", result.stdout)
        self.assertFalse((self.folder / "reports").exists())


if __name__ == "__main__":
    unittest.main()
