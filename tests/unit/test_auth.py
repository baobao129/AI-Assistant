"""认证策略与 Run 事件投递测试。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.application.auth import AuthService
from backend.domain.auth import (
    SessionRecord,
    UserRecord,
    hash_password,
    hash_token,
    is_session_usable,
    issue_token,
    session_expiry,
    verify_password,
)
from backend.domain.errors import AppError
from backend.domain.identifiers import new_id, utc_now


class FakeAuthRepository:
    """内存版认证仓储桩，便于在不接数据库的情况下验证 AuthService 行为。"""

    def __init__(self) -> None:
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}

    def create_user(self, *, username: str, password: str, display_name: str) -> UserRecord:
        password_hash, salt = hash_password(password)
        record = UserRecord(
            user_id=new_id(),
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            password_salt=salt,
            is_active=True,
        )
        self.users[record.username] = record
        return record

    def get_user_by_username(self, username: str) -> UserRecord | None:
        return self.users.get(username)

    def get_user(self, user_id: str) -> UserRecord | None:
        return next((user for user in self.users.values() if user.user_id == user_id), None)

    def create_session(self, *, user_id: str, token: str, expires_at) -> SessionRecord:
        record = SessionRecord(
            session_id=new_id(),
            user_id=user_id,
            token_hash=hash_token(token),
            created_at=utc_now(),
            expires_at=expires_at,
            revoked_at=None,
        )
        self.sessions[record.session_id] = record
        return record

    def get_session_by_token(self, token: str) -> SessionRecord | None:
        digest = hash_token(token)
        return next(
            (session for session in self.sessions.values() if session.token_hash == digest), None
        )

    def revoke_session(self, session_id: str) -> int:
        session = self.sessions.get(session_id)
        if session is None:
            return 0
        self.sessions[session_id] = SessionRecord(
            session_id=session.session_id,
            user_id=session.user_id,
            token_hash=session.token_hash,
            created_at=session.created_at,
            expires_at=session.expires_at,
            revoked_at=utc_now(),
        )
        return 1


def _service(repository: FakeAuthRepository | None = None) -> AuthService:
    # 显式关闭 .env 读取，会话 TTL 固定为 600 秒，保证用例结果可复现。
    from backend.infrastructure.config import Settings

    return AuthService(
        settings=Settings(_env_file=None, auth_session_ttl_seconds=600),
        repository=repository or FakeAuthRepository(),
    )


def test_password_hash_is_salted_and_verified():
    # 同一口令两次加盐应得到不同摘要，且校验必须基于盐值而非明文比较。
    first, salt_a = hash_password("correct horse")
    second, salt_b = hash_password("correct horse")
    assert first != second
    assert salt_a != salt_b
    assert verify_password("correct horse", first, salt_a)
    assert not verify_password("wrong horse", first, salt_a)
    assert not verify_password("correct horse", "md5$1$deadbeef", salt_a)


def test_login_resolves_and_logout_revokes_the_session():
    repository = FakeAuthRepository()
    repository.create_user(username="demo", password="secret", display_name="Demo")
    service = _service(repository)

    result = service.login(username="demo", password="secret")
    assert service.resolve_token(result.token).username == "demo"
    service.logout(result.token)
    with pytest.raises(AppError) as failure:
        service.resolve_token(result.token)
    assert failure.value.code == "authentication_error"


def test_login_rejects_invalid_credentials_and_bootstrap_is_idempotent():
    repository = FakeAuthRepository()
    repository.create_user(username="demo", password="secret", display_name="Demo")
    service = _service(repository)

    with pytest.raises(AppError):
        service.login(username="demo", password="nope")
    with pytest.raises(AppError):
        service.login(username="missing", password="secret")
    assert service.bootstrap_demo_user() is False
    assert len(repository.users) == 1


def test_session_expiry_and_issuance_are_bounded():
    token = issue_token()
    assert len(token) > 20
    assert hash_token(token) != token
    expires_at = session_expiry(60)
    usable = SessionRecord(
        session_id="s",
        user_id="u",
        token_hash=hash_token(token),
        created_at=utc_now(),
        expires_at=expires_at,
        revoked_at=None,
    )
    assert is_session_usable(usable)
    expired = SessionRecord(
        session_id="s2",
        user_id="u",
        token_hash=hash_token(token),
        created_at=utc_now() - timedelta(hours=2),
        expires_at=utc_now() - timedelta(hours=1),
        revoked_at=None,
    )
    assert not is_session_usable(expired)
