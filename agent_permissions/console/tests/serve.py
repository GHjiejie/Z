"""Isolated, temporary real API used only by the Console browser tests."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from agent_permissions.api import create_app
from agent_permissions.config import Settings
from agent_permissions.models import UserCreate
from agent_permissions.security import token_digest
from agent_permissions.service import Authority, RequestInfo
from agent_permissions.test_chat_api import BusinessModel
from agent_permissions.test_support import grant_tools
from agent_permissions.tooling import execution_context
from agent_permissions.tracing import ChatTracing, TracingSettings

ADMIN_PASSWORD = "12345"
MEMBER_PASSWORD = "Console member password 2026!"
INITIAL_PASSWORD = "12345"


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
            chat_model_factory=lambda: BusinessModel(),
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
            prompt = payload["messages"][-1]["content"]

            async def events() -> AsyncIterator[str | bytes]:
                def chunk(
                    text: str | None = None, finish: str | None = None
                ) -> dict[str, object]:
                    return {
                        "id": "chatcmpl-browser",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text} if text is not None else {},
                                "finish_reason": finish,
                            }
                        ],
                    }

                yield b": keepalive\r\n\r\n"
                first_text = (
                    '## 流式 Markdown\n\n**重点**与 `inline_code`。\n\n```python\nprint("hello'
                    if "Markdown" in prompt
                    else "第一段：正在处理你的请求。\n"
                )
                first = (
                    "data: "
                    + json.dumps(chunk(first_text), ensure_ascii=False)
                    + "\r\n\r\n"
                ).encode()
                # Split in the middle of UTF-8 characters, as real network reads may do.
                for start in range(0, len(first), 11):
                    yield first[start : start + 11]
                    await asyncio.sleep(0.002)
                await asyncio.sleep(5 if "慢流" in prompt else 0.8)
                if "错误" in prompt:
                    yield 'data: {"error":{"message":"模型暂时繁忙，请先核对操作结果","code":"model_unavailable"}}\n\n'
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
                        "data: "
                        + json.dumps(chunk(markdown), ensure_ascii=False)
                        + "\n\n"
                    )
                if "长回复" in prompt:
                    for number in range(32):
                        yield (
                            "data: "
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
                    "data: "
                    + json.dumps(chunk("第二段：回复已经完成。"), ensure_ascii=False)
                    + "\n\n"
                )
                yield "data: " + json.dumps(chunk(finish="stop")) + "\n\n"
                yield 'data: {"object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
                yield "data: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")

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
