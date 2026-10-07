"""认证端点（API-002）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response

from backend.api.dependencies import Container, get_container, get_current_user
from backend.api.schemas import LoginRequest, LoginResponse, SessionResponse, UserResponse
from backend.domain.auth import AuthenticatedUser
from backend.domain.identifiers import to_rfc3339

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, container: Container = Depends(get_container)) -> LoginResponse:
    result = container.auth.login(username=payload.username, password=payload.password)
    return LoginResponse(
        user=UserResponse(
            user_id=result.user.user_id,
            username=result.user.username,
            display_name=result.user.display_name,
        ),
        session=SessionResponse(token=result.token, expires_at=to_rfc3339(result.expires_at) or ""),
    )


@router.post("/logout", status_code=204)
def logout(
    container: Container = Depends(get_container),
    authorization: str | None = Header(default=None),
) -> Response:
    # 重复 logout 是幂等的：未知或缺失的 token 仍返回 204。
    if authorization and authorization.lower().startswith("bearer "):
        container.auth.logout(authorization.split(" ", 1)[1].strip())
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def me(user: AuthenticatedUser = Depends(get_current_user)) -> UserResponse:
    return UserResponse(
        user_id=user.user_id, username=user.username, display_name=user.display_name
    )
