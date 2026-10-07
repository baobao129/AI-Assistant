"""Domain 与 Application 层共用的不透明标识符与时间辅助函数。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def new_id() -> str:
    """服务端生成的不透明标识符；客户端不得从中推断任何含义。"""

    return uuid4().hex


def utc_now() -> datetime:
    """用于 MySQL DATETIME(6) 列的朴素（naive）UTC 时间戳。"""

    return datetime.now(UTC).replace(tzinfo=None)


def to_rfc3339(value: datetime | None) -> str | None:
    """API Contract 要求的 RFC 3339 UTC 表示。"""

    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
