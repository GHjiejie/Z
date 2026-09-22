"""Bound the request body before multipart parsing writes an unbounded spool."""

import json

from starlette.exceptions import HTTPException


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="Request body exceeds upload limit")


class RequestSizeLimit:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        sent = False
        size = 0

        async def reject():
            body = json.dumps(
                {
                    "code": "UPLOAD_TOO_LARGE",
                    "message": "请求内容超过大小限制",
                    "request_id": None,
                    "details": None,
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        try:
            length = int(headers.get(b"content-length", b"0"))
            if length > self.max_bytes:
                return await reject()
        except ValueError:
            return await reject()

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                raise BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal sent
            if message["type"] == "http.response.start":
                sent = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyTooLarge:
            if not sent:
                await reject()
