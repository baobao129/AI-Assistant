"""Opaque identifier and time helpers shared by Domain and Application layers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4


def new_id() -> str:
    """Opaque server-generated identifier; clients must not infer meaning from it."""

    return uuid4().hex


def utc_now() -> datetime:
    """Naive UTC timestamp for MySQL DATETIME(6) columns."""

    return datetime.now(UTC).replace(tzinfo=None)


def to_rfc3339(value: datetime | None) -> str | None:
    """RFC 3339 UTC representation required by the API Contract."""

    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
