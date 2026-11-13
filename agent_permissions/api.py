"""FastAPI routes and transport-only contract enforcement."""

import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from agent_permissions.config import Settings
from agent_permissions.console_ui import mount_console
from agent_permissions.database import Database
from agent_permissions.errors import ApiError, invalid_field
from agent_permissions.models import (
    Envelope,
    ErrorResponse,
    LoginRequest,
    LoginResult,
    OwnPermissions,
    PasswordChange,
    PasswordReset,
    PermissionDefinition,
    RoleCreate,
    RoleDefinition,
    RoleOption,
    RolesUpdate,
    RoleUpdate,
    StatusUpdate,
    User,
    UserCreate,
    UserPage,
    UserQuery,
    UserUpdate,
)
from agent_permissions.security import token_digest
from agent_permissions.service import (
    Authority,
    Clock,
    RequestInfo,
    UserService,
    utc_now,
)


class ContractRoute(APIRoute):
    """Reject extra queries and bodies even for routes without model parameters."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()
        allowed_queries = set()
        for parameter in self.dependant.query_params:
            annotation = parameter.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                allowed_queries.update(annotation.model_fields)
            else:
                allowed_queries.add(parameter.alias)

        async def enforce_contract(request: Request) -> Response:
            counts = Counter(key for key, _ in request.query_params.multi_items())
            invalid = [
                key
                for key, count in counts.items()
                if key not in allowed_queries or count > 1
            ]
            if invalid:
                raise invalid_field("query", "存在未定义或重复的查询参数")
            if self.body_field is None:
                if (await request.body()).strip():
                    raise invalid_field("body", "此接口不接受请求体")
            elif request.headers.get("content-type", "").split(";", 1)[0].strip() != (
                "application/json"
            ):
                raise invalid_field("Content-Type", "请求体必须使用 application/json")
            return await handler(request)

        return enforce_contract


bearer = HTTPBearer(auto_error=False)


def current_authority(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Authority:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or len(credentials.credentials) > 1024
    ):
        raise ApiError(401, "UNAUTHENTICATED", "请重新登录")
    return Authority(token_digest(credentials.credentials))


def service_for(request: Request) -> UserService:
    return request.app.state.service


def info_for(request: Request) -> RequestInfo:
    return request.state.info


Service = Annotated[UserService, Depends(service_for)]
Identity = Annotated[Authority, Depends(current_authority)]
Info = Annotated[RequestInfo, Depends(info_for)]
Version = Annotated[
    str | None,
    Header(alias="If-Match", description='修改已有记录时必填，例如 "v3"；缺失返回 428'),
]
UserId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
RoleCode = Annotated[
    str, Path(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_-]{2,63}$")
]


def result(info: RequestInfo, data: object) -> dict:
    return {"data": data, "request_id": info.request_id}


def user_result(response: Response, info: RequestInfo, data: dict) -> dict:
    response.headers["ETag"] = f'"v{data["version"]}"'
    return result(info, data)


router = APIRouter(
    prefix="/api/v1",
    route_class=ContractRoute,
    responses={
        status: {"model": ErrorResponse}
        for status in (400, 401, 403, 404, 409, 412, 422, 428, 429, 500, 503)
    },
)


@router.post("/auth/login", response_model=Envelope[LoginResult], name="auth.login")
def login(payload: LoginRequest, service: Service, info: Info) -> dict:
    data = service.login(payload.username, payload.password.get_secret_value(), info)
    return result(info, data)


@router.post("/auth/logout", status_code=204, name="auth.logout")
def logout(service: Service, identity: Identity, info: Info) -> Response:
    service.logout(identity, info)
    return Response(status_code=204)


@router.get("/users/me", response_model=Envelope[User], name="user.self.read")
def get_me(
    response: Response, service: Service, identity: Identity, info: Info
) -> dict:
    return user_result(response, info, service.get_me(identity, info))


@router.patch("/users/me", response_model=Envelope[User], name="user.self.update")
def update_me(
    payload: UserUpdate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> dict:
    data = service.mutate_user(
        identity,
        "user.self.update",
        None,
        version,
        payload.model_dump(exclude_unset=True),
        info,
    )
    return user_result(response, info, data)


@router.put("/users/me/password", status_code=204, name="user.self.password.change")
def change_password(
    payload: PasswordChange,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> Response:
    service.mutate_user(
        identity,
        "user.self.password.change",
        None,
        version,
        payload.model_dump(),
        info,
    )
    return Response(status_code=204)


@router.get(
    "/users/me/permissions",
    response_model=Envelope[OwnPermissions],
    name="user.self.permissions",
)
def own_permissions(service: Service, identity: Identity, info: Info) -> dict:
    return result(info, service.own_permissions(identity, info))


@router.post(
    "/users", status_code=201, response_model=Envelope[User], name="user.create"
)
def create_user(
    payload: UserCreate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
) -> dict:
    data = service.create_user(identity, payload, info)
    response.headers["Location"] = f"/api/v1/users/{data['id']}"
    return user_result(response, info, data)


@router.get("/users", response_model=Envelope[UserPage], name="user.list")
def list_users(
    query: Annotated[UserQuery, Query()],
    service: Service,
    identity: Identity,
    info: Info,
) -> dict:
    return result(info, service.list_users(identity, query, info))


@router.get("/users/{user_id}", response_model=Envelope[User], name="user.read")
def get_user(
    user_id: UserId,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
) -> dict:
    return user_result(response, info, service.get_user(identity, user_id, info))


@router.patch("/users/{user_id}", response_model=Envelope[User], name="user.update")
def update_user(
    user_id: UserId,
    payload: UserUpdate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> dict:
    data = service.mutate_user(
        identity,
        "user.update",
        user_id,
        version,
        payload.model_dump(exclude_unset=True),
        info,
    )
    return user_result(response, info, data)


@router.delete("/users/{user_id}", status_code=204, name="user.delete")
def delete_user(
    user_id: UserId,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> Response:
    service.mutate_user(identity, "user.delete", user_id, version, {}, info)
    return Response(status_code=204)


@router.put(
    "/users/{user_id}/status",
    response_model=Envelope[User],
    name="user.status.update",
)
def update_status(
    user_id: UserId,
    payload: StatusUpdate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> dict:
    data = service.mutate_user(
        identity, "user.status.update", user_id, version, payload.model_dump(), info
    )
    return user_result(response, info, data)


@router.put("/users/{user_id}/password", status_code=204, name="user.password.reset")
def reset_password(
    user_id: UserId,
    payload: PasswordReset,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> Response:
    service.mutate_user(
        identity, "user.password.reset", user_id, version, payload.model_dump(), info
    )
    return Response(status_code=204)


@router.put(
    "/users/{user_id}/roles", response_model=Envelope[User], name="user.roles.assign"
)
def assign_roles(
    user_id: UserId,
    payload: RolesUpdate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> dict:
    data = service.mutate_user(
        identity, "user.roles.assign", user_id, version, payload.model_dump(), info
    )
    return user_result(response, info, data)


@router.get("/roles", response_model=Envelope[list[RoleDefinition]], name="role.list")
def list_roles(service: Service, identity: Identity, info: Info) -> dict:
    return result(info, service.roles(identity, info))


@router.get(
    "/role-options", response_model=Envelope[list[RoleOption]], name="role.options"
)
def role_options(service: Service, identity: Identity, info: Info) -> dict:
    return result(info, service.role_options(identity, info))


@router.get(
    "/permissions",
    response_model=Envelope[list[PermissionDefinition]],
    name="permission.list",
)
def list_permissions(service: Service, identity: Identity, info: Info) -> dict:
    return result(info, service.permission_catalog(identity, info))


@router.post(
    "/roles",
    response_model=Envelope[RoleDefinition],
    status_code=201,
    name="role.create",
)
def create_role(
    payload: RoleCreate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
) -> dict:
    data = service.mutate_role(
        identity, "role.create", payload.code, None, payload.model_dump(), info
    )
    response.headers["Location"] = f"/api/v1/roles/{payload.code}"
    return user_result(response, info, data)


@router.get(
    "/roles/{role_code}", response_model=Envelope[RoleDefinition], name="role.read"
)
def get_role(
    role_code: RoleCode,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
) -> dict:
    return user_result(response, info, service.get_role(identity, role_code, info))


@router.patch(
    "/roles/{role_code}", response_model=Envelope[RoleDefinition], name="role.update"
)
def update_role(
    role_code: RoleCode,
    payload: RoleUpdate,
    response: Response,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> dict:
    data = service.mutate_role(
        identity,
        "role.update",
        role_code,
        version,
        payload.model_dump(exclude_unset=True),
        info,
    )
    return user_result(response, info, data)


@router.delete("/roles/{role_code}", status_code=204, name="role.delete")
def delete_role(
    role_code: RoleCode,
    service: Service,
    identity: Identity,
    info: Info,
    version: Version = None,
) -> Response:
    service.mutate_role(identity, "role.delete", role_code, version, {}, info)
    return Response(status_code=204)


def error_response(request: Request, error: ApiError) -> JSONResponse:
    info = request.state.info
    route = request.scope.get("route")
    if route and getattr(route, "path", "").startswith("/api/v1/"):
        request.app.state.service.record_failure(
            info,
            route.name,
            request.path_params.get("user_id") or info.actor_user_id,
            error.code,
            role_code=request.path_params.get("role_code"),
        )
    headers = {
        "Cache-Control": "no-store",
        "X-Request-ID": info.request_id,
        **error.headers,
    }
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=error.status,
        headers=headers,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            },
            "request_id": info.request_id,
        },
    )


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock = utc_now,
    console_directory: FilePath | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    service = UserService(database, settings, clock=clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(database.initialize)
        yield

    app = FastAPI(
        title="Agent 用户管理服务",
        version="1.1.0",
        description="用户、会话与系统角色管理；接口契约见用户管理服务文档。",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.service = service
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.info = RequestInfo(
            f"req_{uuid4().hex}", request.client.host if request.client else "unknown"
        )
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 -- final HTTP boundary must redact all errors
            # Keep framework exception strings and request inputs out of errors.
            response = await run_in_threadpool(
                error_response,
                request,
                ApiError(500, "INTERNAL_ERROR", "服务内部错误，请凭请求标识排查"),
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.info.request_id
        return response

    @app.exception_handler(ApiError)
    def application_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"],
            }
            for item in error.errors()
        ]
        return error_response(
            request,
            ApiError(422, "VALIDATION_ERROR", "请求参数不符合要求", details=details),
        )

    @app.exception_handler(sqlite3.Error)
    def persistence_error(request: Request, error: sqlite3.Error) -> JSONResponse:
        return error_response(
            request,
            ApiError(503, "SERVICE_UNAVAILABLE", "暂时无法完成请求，请稍后重试"),
        )

    @app.exception_handler(HTTPException)
    def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request,
            ApiError(
                error.status_code,
                "NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
                "接口不存在" if error.status_code == 404 else "HTTP 请求不符合要求",
                headers=error.headers,
            ),
        )

    app.include_router(router)
    mount_console(app, console_directory)
    return app
