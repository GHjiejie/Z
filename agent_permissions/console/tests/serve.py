"""Isolated, temporary real API used only by the Console browser tests."""

import asyncio
import codecs
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from starlette.responses import StreamingResponse

from agent_permissions.agui import AguiContext, encode_agui
from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.security import token_digest
from agent_permissions.service import Authority, RequestInfo
from agent_permissions.test_chat_api import BusinessModel
from agent_permissions.test_hitl import InteractionModel
from agent_permissions.test_support import grant_tools
from agent_permissions.tooling import execution_context
from agent_permissions.tracing import ChatTracing, TracingSettings

ADMIN_PASSWORD = "12345"
MEMBER_PASSWORD = "Console member password 2026!"
INITIAL_PASSWORD = "12345"


class ConsoleModel(BusinessModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        prompt = next(
            (str(item.content) for item in reversed(messages) if item.type == "human"),
            "",
        )
        if prompt.startswith("AGUI "):
            for summary in ("先核对当前账号权限。", "再查询当前账号资料。"):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=[{"type": "reasoning", "reasoning": summary}]
                    )
                )
                time.sleep(0.6)
        message = (
            self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=message.content,
                tool_calls=message.tool_calls,
                usage_metadata=message.usage_metadata,
            )
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        prompt = next(
            (str(item.content) for item in reversed(messages) if item.type == "human"),
            "",
        )
        if prompt.startswith("HITL "):
            kind = prompt.split(" ")[1]
            fixture = InteractionModel(model_name="fixture-model")
            if kind == "创建用户":
                fixture.operation, fixture.arguments, fixture.prepare = (
                    "user.create",
                    {},
                    True,
                )
            elif kind == "删除角色":
                fixture.operation, fixture.arguments, fixture.prepare = (
                    "role.delete",
                    {},
                    True,
                )
            elif kind == "查询用户":
                fixture.operation, fixture.arguments, fixture.prepare = (
                    "user.read",
                    {},
                    True,
                )
            else:
                fixture.arguments = {
                    "body": {
                        "code": prompt.split(" ")[-1],
                        "display_name": "待确认角色",
                    }
                }
            return fixture._generate(messages, stop, run_manager, **kwargs)
        return super()._generate(messages, stop, run_manager, **kwargs)


def main() -> None:
    with TemporaryDirectory(prefix="agent-console-browser-") as temporary:
        settings = Settings(
            database_path=Path(temporary) / "browser.sqlite3",
            login_account_limit=1000,
            login_source_limit=1000,
            password_limit=1000,
        )
        app = create_app(
            settings,
            chat_model_factory=lambda: ConsoleModel(),
            chat_tracing=ChatTracing(TracingSettings()),
        )

        @app.post(
            "/__test/chat-stream",
            response_model=None,
            openapi_extra={
                "x-agent": {"expose": False, "reason": "Browser-only SSE fixture"}
            },
        )
        async def chat_stream(request: Request) -> StreamingResponse:
            await execution_context(request)
            payload = await request.json()
            agui = (
                AguiContext(payload["threadId"], payload["runId"])
                if "threadId" in payload
                else None
            )
            if agui:
                payload = {
                    "messages": payload["messages"],
                    "conversation_id": agui.thread_id,
                    "client_request_id": agui.run_id,
                }
            prompt = payload["messages"][-1]["content"]

            async def events() -> AsyncIterator[str | bytes]:
                run = {
                    "id": "fixture_" + uuid4().hex,
                    "conversation_id": payload["conversation_id"],
                    "client_request_id": payload["client_request_id"],
                    "status": "running",
                    "version": 1,
                    "content": "",
                    "finish": "stop",
                    "usage": None,
                    "error": None,
                    "expires_at": 9999999999,
                    "purged": False,
                    "interaction": None,
                    "executions": [],
                }
                sequence = 0

                def event(kind, **data):
                    nonlocal sequence
                    sequence += 1
                    return {
                        "type": kind,
                        "run_id": run["id"],
                        "stream_id": run["id"],
                        "sequence": sequence,
                        "protocol_version": 1,
                        **data,
                    }

                def frame(kind, **data):
                    return (
                        "event: "
                        + kind
                        + "\ndata: "
                        + json.dumps(event(kind, **data), ensure_ascii=False)
                        + "\n\n"
                    )

                def chunk(text):
                    run["content"] += text
                    return event("text.delta", text=text)

                yield frame("run.started", run=run)

                yield b": keepalive\r\n\r\n"
                first_text = (
                    '## 流式 Markdown\n\n**重点**与 `inline_code`。\n\n```python\nprint("hello'
                    if "Markdown" in prompt
                    else "第一段：正在处理你的请求。\n"
                )
                first = (
                    "event: text.delta\ndata: "
                    + json.dumps(chunk(first_text), ensure_ascii=False)
                    + "\r\n\r\n"
                ).encode()
                # Split in the middle of UTF-8 characters, as real network reads may do.
                for start in range(0, len(first), 11):
                    yield first[start : start + 11]
                    await asyncio.sleep(0.002)
                await asyncio.sleep(5 if "慢流" in prompt else 0.8)
                if "错误" in prompt:
                    run.update(
                        status="failed",
                        version=2,
                        error={
                            "message": "模型暂时繁忙，请先核对操作结果",
                            "code": "model_unavailable",
                        },
                    )
                    yield frame("run.failed", run=run)
                    yield frame("stream.closed", status="failed")
                    return
                if "断流" in prompt:
                    return
                if "Markdown" in prompt:
                    markdown = (
                        ' Markdown")\nprint("<safe>")\n```\n\n'
                        "> 这是一段引用说明。\n\n"
                        "- 普通条目\n- ~~删除的内容~~\n\n"
                        "1. 第一步\n2. 第二步\n\n"
                        "- [x] 已完成\n- [ ] 待处理\n\n"
                        "| 用户名 | 角色 | 状态 | 说明 |\n"
                        "| --- | --- | --- | --- |\n"
                        "| alice | 管理员 | 启用 | 测试记录 |\n\n"
                        "[项目文档](https://example.com/guide)\n\n"
                        "[危险链接](javascript:alert%281%29)\n\n"
                        "<script>window.markdownInjected = true</script>\n\n"
                        '<img src="x" onerror="window.markdownInjected = true">\n\n'
                        "**渲染完成**\n\n"
                    )
                    yield (
                        "event: text.delta\ndata: "
                        + json.dumps(chunk(markdown), ensure_ascii=False)
                        + "\n\n"
                    )
                if "长回复" in prompt:
                    for number in range(32):
                        yield (
                            "event: text.delta\ndata: "
                            + json.dumps(
                                chunk(
                                    f"第 {number + 1} 项：这是一段用于检验阅读位置的长回复。\n\n"
                                ),
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        await asyncio.sleep(0.06)
                yield (
                    "event: text.delta\ndata: "
                    + json.dumps(chunk("第二段：回复已经完成。"), ensure_ascii=False)
                    + "\n\n"
                )
                run.update(
                    status="completed",
                    version=2,
                    usage={
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    },
                )
                yield frame("run.completed", run=run)
                yield frame("stream.closed", status="completed")

            async def protocol_events():
                decoder = codecs.getincrementaldecoder("utf-8")()
                buffer = ""
                async for part in events():
                    buffer += decoder.decode(part) if isinstance(part, bytes) else part
                    buffer = buffer.replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        data = [
                            line[6:]
                            for line in frame.splitlines()
                            if line.startswith("data: ")
                        ]
                        if data:
                            yield json.loads("\n".join(data))

            async def agui_frames():
                async for frame in encode_agui(protocol_events(), agui):
                    if "TEXT_MESSAGE_CONTENT" in frame:
                        raw = frame.encode()
                        for start in range(0, len(raw), 19):
                            yield raw[start : start + 19]
                            await asyncio.sleep(0.002)
                    else:
                        yield frame

            return StreamingResponse(
                agui_frames() if agui else events(), media_type="text/event-stream"
            )

        app.state.database.initialize()
        service = app.state.service
        service.bootstrap_admin(
            UserCreate(
                username="console_admin",
                display_name="系统管理员",
                password=ADMIN_PASSWORD,
            )
        )
        login = service.login(
            "console_admin",
            ADMIN_PASSWORD,
            RequestInfo("browser-test-fixture", "local-test"),
        )
        identity = Authority(token_digest(login["session_token"]))
        for username, name, role in (
            ("console_member", "普通成员", "member"),
            ("first_login", "新加入的成员", "member"),
            ("role_admin", "权限管理员", "admin"),
        ):
            service.create_user(
                identity,
                UserCreate(
                    username=username,
                    display_name=name,
                    password=MEMBER_PASSWORD
                    if username == "console_member"
                    else INITIAL_PASSWORD,
                    roles=[role],
                ),
                RequestInfo("browser-test-fixture", "local-test"),
            )
        # Browser Chat fixtures explicitly opt in; newly created users remain ungranted.
        from agent_permissions.tooling import activate_tools

        activate_tools(app)
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer " + login["session_token"]}
            for user in client.get("/api/v1/users", headers=headers).json()["data"][
                "items"
            ]:
                grant_tools(client, headers, user["id"])
        uvicorn.run(
            app, host="127.0.0.1", port=18765, proxy_headers=False, access_log=False
        )


if __name__ == "__main__":
    main()
