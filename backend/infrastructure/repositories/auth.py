"""Authentication persistence: users and bearer sessions."""

from __future__ import annotations

from datetime import datetime

from backend.domain.auth import SessionRecord, UserRecord, hash_password, hash_token
from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool


class AuthRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    # Users -----------------------------------------------------------------
    def get_user_by_username(self, username: str) -> UserRecord | None:
        row = self.pool.query_one(
            "SELECT user_id, username, display_name, password_hash, password_salt, is_active "
            "FROM users WHERE username = %s",
            (username,),
        )
        return _user(row)

    def get_user(self, user_id: str) -> UserRecord | None:
        row = self.pool.query_one(
            "SELECT user_id, username, display_name, password_hash, password_salt, is_active "
            "FROM users WHERE user_id = %s",
            (user_id,),
        )
        return _user(row)

    def create_user(self, *, username: str, password: str, display_name: str) -> UserRecord:
        password_hash, salt = hash_password(password)
        user_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO users (user_id, username, display_name, password_hash, password_salt, "
            "is_active, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 1, %s, %s)",
            (user_id, username, display_name, password_hash, salt, now, now),
        )
        return UserRecord(
            user_id=user_id,
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            password_salt=salt,
            is_active=True,
        )

    def count_users(self) -> int:
        row = self.pool.query_one("SELECT COUNT(*) FROM users")
        return int(row[0]) if row else 0

    # Sessions --------------------------------------------------------------
    def create_session(self, *, user_id: str, token: str, expires_at: datetime) -> SessionRecord:
        session_id = new_id()
        now = utc_now()
        token_hash = hash_token(token)
        self.pool.execute(
            "INSERT INTO auth_sessions (session_id, user_id, token_hash, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, user_id, token_hash, now, expires_at),
        )
        return SessionRecord(
            session_id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
        )

    def get_session_by_token(self, token: str) -> SessionRecord | None:
        row = self.pool.query_one(
            "SELECT session_id, user_id, token_hash, created_at, expires_at, revoked_at "
            "FROM auth_sessions WHERE token_hash = %s",
            (hash_token(token),),
        )
        if row is None:
            return None
        return SessionRecord(
            session_id=row[0],
            user_id=row[1],
            token_hash=row[2],
            created_at=row[3],
            expires_at=row[4],
            revoked_at=row[5],
        )

    def revoke_session(self, session_id: str) -> int:
        return self.pool.execute(
            "UPDATE auth_sessions SET revoked_at = %s WHERE session_id = %s AND revoked_at IS NULL",
            (utc_now(), session_id),
        )


def _user(row: tuple[object, ...] | None) -> UserRecord | None:
    if row is None:
        return None
    return UserRecord(
        user_id=str(row[0]),
        username=str(row[1]),
        display_name=str(row[2]),
        password_hash=str(row[3]),
        password_salt=str(row[4]),
        is_active=bool(row[5]),
    )
