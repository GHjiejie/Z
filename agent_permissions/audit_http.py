"""Audit the two real HTTP boundaries without accepting client-supplied attribution."""

import json
import time
from dataclasses import replace

from fastapi.exceptions import RequestValidationError

from agent_permissions import audit
from agent_permissions.errors import ApiError


async def gateway(request, handler):
    parent = audit.active.get()
    info = request.state.info
    if parent:
        call = replace(
            parent,
            fields=dict(parent.fields),
            checks=[],
            grant={},
            stage="tool_entry",
            checks_recorded=False,
        )
    else:
        call = audit.Call(
            request.app.state.database,
            request.app.state.service.clock,
            "direct_tool_api",
            {"purpose": "operation"},
            stage="tool_entry",
        )
    call.fields["control_request_id"] = info.request_id
    with audit.scope(call):
        try:
            response = await handler(request)
        except Exception:
            await call.reject(ApiError(500, "INTERNAL_ERROR", ""))
            raise
        if call.fields.get("operation_id") != "auth.login":
            call.identify(info.actor_user_id)
        if response.status_code >= 400:
            if (
                response.status_code == 422
                and not info.actor_user_id
                and request.headers.get("authorization")
            ):
                from agent_permissions.tooling import _execution_access

                try:
                    await _execution_access(request)
                    call.identify(info.actor_user_id)
                except ApiError:
                    pass
            error = ApiError(
                response.status_code,
                getattr(request.state, "audit_reason", "HTTP_ERROR"),
                "",
            )
            await call.reject(error)
        else:
            await call.emit(
                "control.returned", control_http_status=response.status_code
            )
        response.headers["X-Agent-Call-ID"] = call.call_id
        return response


async def before_dispatch(request):
    call = audit.active.get()
    if call:
        await call.emit("dispatch.attempted")
        call.dispatched = True


async def business(request, handler, operation):
    parent = audit.active.get()
    if not parent:
        return await handler(request)
    info = request.state.info
    if (
        parent.stage == "tool_entry"
        and parent.dispatched
        and parent.fields.get("operation_id") == operation
    ):
        call = replace(parent, fields=dict(parent.fields), checks=[], stage="business")
    else:
        if not audit.form_read.get() or parent.stage == "business":
            return await handler(request)
        snapshot = request.app.state.tool_registry.snapshot()
        tool = next(
            (t for t in snapshot["tools"] if t["binding"]["operationId"] == operation),
            None,
        )
        if not tool:
            return await handler(request)
        inherited = {
            k: v
            for k, v in parent.fields.items()
            if k
            in {
                "run_id",
                "interaction_id",
                "agent_id",
                "actor_user_id",
                "subject_user_id",
                "control_request_id",
            }
        }
        call = audit.Call(
            parent.database,
            parent.clock,
            parent.entrypoint,
            {**inherited, "purpose": "form_lookup", "parent_call_id": parent.call_id},
            stage="business",
        )
        call.bind(tool, snapshot["bundleDigest"])
    call.fields["business_request_id"] = info.request_id
    with audit.scope(call):
        started = time.monotonic()
        await call.emit("business.received")
        try:
            response = await handler(request)
        except Exception as exc:
            if isinstance(exc, ApiError):
                status, code = exc.status, exc.code
            elif isinstance(exc, RequestValidationError):
                status, code = 422, "VALIDATION_ERROR"
            elif isinstance(exc, audit.DB_ERRORS):
                status, code = 503, "SERVICE_UNAVAILABLE"
            else:
                status, code = 500, "INTERNAL_ERROR"
            call.identify(info.actor_user_id)
            if status == 401:
                call.checks.append(
                    {
                        "stage": "business",
                        "check": "authentication",
                        "decision": "denied",
                    }
                )
            try:
                await call.emit(
                    "result.unknown"
                    if status >= 500 and code != "AUDIT_UNAVAILABLE"
                    else "business.failed",
                    reason_code=code,
                    business_http_status=status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except ApiError:
                if status not in (401, 403):
                    raise
                audit.logger.error(
                    "Business denial audit unavailable request_id=%s", info.request_id
                )
            raise
        call.identify(info.actor_user_id)
        details = {
            "business_http_status": response.status_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if call.fields.get("effect") == "read":
            data = json.loads(response.body)["data"] if response.body else None
            if isinstance(data, dict):
                target = data.get("id") or (
                    data.get("code") if operation == "role.read" else None
                )
                if target or operation.startswith("user.self."):
                    details["target_id"] = target or info.actor_user_id
                if operation == "user.list":
                    details["result_summary"] = {
                        "page": data["page"],
                        "page_size": data["page_size"],
                    }
                data = data.get("items")
            if isinstance(data, list):
                details.setdefault("result_summary", {})["returned_count"] = len(data)
            await call.emit("read.succeeded", **details)
        else:
            # Success is proved solely by the event inserted in the business transaction.
            await call.emit("business.returned", permission_checks=[], **details)
        return response
