from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response

from apps.platform_api.dependencies import (
    authenticated_principal,
    require_user_manager,
    services,
)
from packages.auth import (
    AuthAuditListResponse,
    AuthSessionListResponse,
    AuthenticatedPrincipal,
    LoginRequest,
    LoginResponse,
    OkResponse,
    PasswordChangeRequest,
    PasswordResetRequest,
    SessionMutationResponse,
    UserCreate,
    UserDeleteRequest,
    UserListResponse,
    UserResponse,
    UserUpdate,
)


router = APIRouter(prefix="/api/v1")


def _request_metadata(request: Request) -> dict[str, Optional[str]]:
    return {
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent", "")[:500] or None,
    }


def _clear_session_cookie(response: Response, auth) -> None:
    response.delete_cookie(
        key=auth.cookie_name,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/auth/login", response_model=LoginResponse)
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    container=Depends(services),
):
    session = container.auth.login(
        payload.username, payload.password, _request_metadata(request)
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.set_cookie(
        key=container.auth.cookie_name,
        value=session["access_token"],
        max_age=session["expires_in"],
        httponly=True,
        secure=container.auth.cookie_secure,
        samesite="lax",
        path="/",
    )
    return session


@router.post("/auth/logout", response_model=OkResponse)
def logout(
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    container.auth.logout(principal, _request_metadata(request))
    _clear_session_cookie(response, container.auth)
    return {"ok": True}


@router.get("/auth/me", response_model=UserResponse)
def current_user(
    response: Response,
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    response.headers["Cache-Control"] = "no-store"
    return container.auth.get_user(principal.user_id)


@router.put("/auth/password", response_model=UserResponse)
def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    return container.auth.change_own_password(
        payload, principal, _request_metadata(request)
    )


@router.get("/auth/sessions", response_model=AuthSessionListResponse)
def own_sessions(
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    return container.auth.list_sessions(
        principal.user_id, principal, principal.session_id
    )


@router.delete(
    "/auth/sessions/{session_id}", response_model=SessionMutationResponse
)
def revoke_own_session(
    request: Request,
    session_id: str,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    result = container.auth.revoke_managed_session(
        principal.user_id,
        session_id,
        principal,
        _request_metadata(request),
    )
    if result["revoked_current"]:
        _clear_session_cookie(response, container.auth)
    return result


@router.delete("/auth/sessions", response_model=SessionMutationResponse)
def revoke_all_own_sessions(
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(authenticated_principal),
    container=Depends(services),
):
    result = container.auth.revoke_all_managed_sessions(
        principal.user_id, principal, _request_metadata(request)
    )
    if result["revoked_current"]:
        _clear_session_cookie(response, container.auth)
    return result


@router.get("/users", response_model=UserListResponse)
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: Optional[str] = Query(default=None, max_length=100),
    status: Optional[Literal["ACTIVE", "INACTIVE", "ALL"]] = None,
    role: Optional[str] = Query(default=None, max_length=32),
    tenant_id: Optional[str] = Query(default=None, max_length=100),
    project_id: Optional[str] = Query(default=None, max_length=100),
    sort_by: Literal[
        "username",
        "display_name",
        "status",
        "created_at",
        "updated_at",
        "last_login_at",
    ] = "username",
    sort_order: Literal["asc", "desc"] = "asc",
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.list_users(
        principal,
        page=page,
        page_size=page_size,
        query=q,
        status=status,
        role=role,
        tenant_id=tenant_id,
        project_id=project_id,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.post("/users", status_code=201, response_model=UserResponse)
def create_user(
    request: Request,
    payload: UserCreate,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.create_user(
        payload, principal, _request_metadata(request)
    )


@router.get("/users/audit-events", response_model=AuthAuditListResponse)
def list_audit_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    q: Optional[str] = Query(default=None, max_length=100),
    action: Optional[str] = Query(default=None, max_length=100),
    outcome: Optional[str] = Query(default=None, max_length=32),
    target_user_id: Optional[str] = Query(default=None, max_length=100),
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.list_audit_events(
        principal,
        page=page,
        page_size=page_size,
        query=q,
        action=action,
        outcome=outcome,
        target_user_id=target_user_id,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.get_managed_user(user_id, principal)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    request: Request,
    user_id: str,
    payload: UserUpdate,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.update_user(
        user_id, payload, principal, _request_metadata(request)
    )


@router.put("/users/{user_id}/password", response_model=UserResponse)
def reset_user_password(
    request: Request,
    user_id: str,
    payload: PasswordResetRequest,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.reset_password(
        user_id, payload, principal, _request_metadata(request)
    )


@router.delete("/users/{user_id}", response_model=UserResponse)
def deactivate_user(
    request: Request,
    user_id: str,
    payload: UserDeleteRequest,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.deactivate_user(
        user_id, payload, principal, _request_metadata(request)
    )


@router.get(
    "/users/{user_id}/sessions", response_model=AuthSessionListResponse
)
def managed_user_sessions(
    user_id: str,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    return container.auth.list_sessions(user_id, principal, principal.session_id)


@router.delete(
    "/users/{user_id}/sessions/{session_id}",
    response_model=SessionMutationResponse,
)
def revoke_managed_user_session(
    request: Request,
    user_id: str,
    session_id: str,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    result = container.auth.revoke_managed_session(
        user_id, session_id, principal, _request_metadata(request)
    )
    if result["revoked_current"]:
        _clear_session_cookie(response, container.auth)
    return result


@router.delete(
    "/users/{user_id}/sessions", response_model=SessionMutationResponse
)
def revoke_all_managed_user_sessions(
    request: Request,
    user_id: str,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(require_user_manager),
    container=Depends(services),
):
    result = container.auth.revoke_all_managed_sessions(
        user_id, principal, _request_metadata(request)
    )
    if result["revoked_current"]:
        _clear_session_cookie(response, container.auth)
    return result
