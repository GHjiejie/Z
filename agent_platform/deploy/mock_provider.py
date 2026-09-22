"""Local protocol fixture for smoke_test.py; never used by the application."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTS = {"calls": 0, "retry_test": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(COUNTS).encode())

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        COUNTS["calls"] += 1
        messages = body.get("messages", [])
        if any(message.get("content") == "retry-test" for message in messages):
            COUNTS["retry_test"] += 1
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":{"message":"simulated failure","type":"internal_error"}}'
            )
            return

        def chunk(delta=None, finish=None, usage=None):
            return {
                "id": "chatcmpl-smoke",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": []
                if usage
                else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                **({"usage": usage} if usage else {}),
            }

        if body.get("tools") and messages[-1]["role"] != "tool":
            chunks = [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "smoke-tool",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression":',
                                },
                            }
                        ],
                    }
                ),
                chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"2+2"}'}}]}
                ),
                chunk(finish="tool_calls"),
            ]
        else:
            chunks = [
                chunk({"role": "assistant", "content": "Answer: 4"}),
                chunk(finish="stop"),
            ]
        if not any(
            message.get("content") == "missing-usage-test" for message in messages
        ):
            chunks.append(
                chunk(
                    usage={
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                    }
                )
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for item in chunks:
            self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
