"""最小化的应用认证策略（ADR-018）。

MVP 仅提供登录、登出和基于会话令牌的当前用户解析，刻意不引入 RBAC、OAuth 与
SSO。密码与令牌材料在本模块内只以哈希形式处理；明文永不离开请求边界。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.domain.identifiers import new_id, utc_now

PBKDF2_ITERATIONS = 240_000
PASSWORD_ALGORITHM = "pbkdf2_sha256"
TOKEN_BYTES = 32


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    display_name: str


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    username: str
    display_name: str
    password_hash: str
    password_salt: str
    is_active: bool


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    user_id: str
    token_hash: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """派生加盐的 PBKDF2 哈希；返回 ``(hash, salt)``。"""

    resolved_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), resolved_salt.encode("utf-8"), PBKDF2_ITERATIONS
    )
    return f"{PASSWORD_ALGORITHM}${PBKDF2_ITERATIONS}${digest.hex()}", resolved_salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    # 用常量时间比较（hmac.compare_digest）避免通过比较耗时泄露哈希信息；
    # 哈希格式或算法不匹配一律视为校验失败，不抛出异常。
    try:
        algorithm, iterations, digest = password_hash.split("$", 2)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def session_expiry(ttl_seconds: int, *, now: datetime | None = None) -> datetime:
    return (now or utc_now()) + timedelta(seconds=ttl_seconds)


def new_session_id() -> str:
    return new_id()


def is_session_usable(session: SessionRecord, *, now: datetime | None = None) -> bool:
    moment = now or utc_now()
    return session.revoked_at is None and session.expires_at > moment
