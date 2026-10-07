"""最小认证服务（ADR-018）。"""

from __future__ import annotations

import logging

from backend.domain.auth import (
    AuthenticatedUser,
    SessionRecord,
    UserRecord,
    is_session_usable,
    issue_token,
    session_expiry,
    verify_password,
)
from backend.domain.errors import AppError, authentication_error
from backend.infrastructure.config import Settings
from backend.infrastructure.repositories.auth import AuthRepository

logger = logging.getLogger(__name__)


class LoginResult:
    def __init__(self, user: AuthenticatedUser, token: str, expires_at) -> None:
        self.user = user
        self.token = token
        self.expires_at = expires_at


class AuthService:
    def __init__(self, *, settings: Settings, repository: AuthRepository) -> None:
        self.settings = settings
        self.repository = repository

    def login(self, *, username: str, password: str) -> LoginResult:
        record = self.repository.get_user_by_username(username)
        if record is None or not record.is_active:
            raise authentication_error("Invalid credentials.")
        if not verify_password(password, record.password_hash, record.password_salt):
            raise authentication_error("Invalid credentials.")
        token = issue_token()
        expires_at = session_expiry(self.settings.auth_session_ttl_seconds)
        self.repository.create_session(user_id=record.user_id, token=token, expires_at=expires_at)
        return LoginResult(_user(record), token, expires_at)

    def resolve_token(self, token: str) -> AuthenticatedUser:
        if not token:
            raise authentication_error()
        session = self.repository.get_session_by_token(token)
        if session is None or not is_session_usable(session):
            raise authentication_error("The session is invalid or expired.")
        record = self.repository.get_user(session.user_id)
        if record is None or not record.is_active:
            raise authentication_error("The session is invalid or expired.")
        return _user(record)

    def logout(self, token: str) -> None:
        session: SessionRecord | None = self.repository.get_session_by_token(token)
        if session is None:
            return
        self.repository.revoke_session(session.session_id)

    def bootstrap_demo_user(self) -> bool:
        """仅创建一次配置的 demo 用户；绝不覆盖已存在的账号。"""

        username = self.settings.auth_bootstrap_username.strip()
        password = self.settings.auth_bootstrap_password.get_secret_value()
        if not username or not password:
            return False
        if self.repository.get_user_by_username(username) is not None:
            return False
        self.repository.create_user(
            username=username,
            password=password,
            display_name=self.settings.auth_bootstrap_display_name[:128],
        )
        logger.info("bootstrap_user_created")
        return True


def _user(record: UserRecord) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=record.user_id, username=record.username, display_name=record.display_name
    )


__all__ = ["AuthService", "AuthenticatedUser", "AppError", "LoginResult"]
