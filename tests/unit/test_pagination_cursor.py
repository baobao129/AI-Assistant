"""Naive UTC cursors preserve repository pagination across host/session timezones.

Only the database boundary is replaced; the three production list methods and
record mappers execute unchanged. No provider or database is contacted.
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from backend.domain.identifiers import cursor_from_utc, cursor_to_utc
from backend.infrastructure.repositories.conversation import ConversationRepository
from backend.infrastructure.repositories.documents import DocumentRepository


@pytest.mark.parametrize(
    ("value", "cursor"),
    [
        (datetime(2026, 1, 1, 12, 34, 56, 123456), "1767270896123456"),
        (datetime(1970, 1, 1, 0, 0, 0, 1), "1"),
        (datetime(1969, 12, 31, 23, 59, 59, 999999), "-1"),
        (datetime(9999, 12, 31, 23, 59, 59, 999999), "253402300799999999"),
    ],
)
def test_cursor_preserves_utc_microseconds(value: datetime, cursor: str):
    assert cursor_from_utc(value) == cursor
    assert cursor_to_utc(cursor) == value


@pytest.mark.parametrize("offset", [-5, 0, 8])
def test_aware_cursor_converts_the_instant_to_utc(offset: int):
    utc = datetime(2026, 1, 1, 12, 34, 56, 123456, tzinfo=UTC)
    value = utc.astimezone(timezone(timedelta(hours=offset)))
    assert cursor_from_utc(value) == "1767270896123456"
    assert cursor_to_utc(cursor_from_utc(value)) == utc.replace(tzinfo=None)


class LocalDatabaseTime(datetime):
    """Model naive datetime.timestamp() on a non-UTC host deterministically."""

    host_offset = timedelta(hours=8)

    def timestamp(self) -> float:
        if self.tzinfo is not None:
            return super().timestamp()
        return (self.replace(tzinfo=UTC) - self.host_offset).timestamp()


class PagePool:
    """Bound read-only database boundary with real filtering of fixture rows."""

    def __init__(self, rows: list[tuple[Any, ...]], timestamp_column: int):
        self.rows = rows
        self.timestamp_column = timestamp_column
        self.boundaries: list[datetime] = []

    def query(self, sql: str, parameters: Any = ()) -> list[tuple[Any, ...]]:
        assert parameters[0] in {"user-1", "conversation-1"}
        rows = self.rows
        if len(parameters) == 3:
            boundary = parameters[1]
            # DATETIME values must be bound directly, so MySQL does not decode
            # them using FROM_UNIXTIME/session timezone conversion.
            assert isinstance(boundary, datetime)
            assert boundary.tzinfo is None
            self.boundaries.append(boundary)
            rows = [row for row in rows if row[self.timestamp_column] < boundary]
        return rows[: parameters[-1]]


def row_for(kind: str, identity: str, created: datetime) -> tuple[Any, ...]:
    if kind == "documents":
        return (
            identity,
            "user-1",
            "Title",
            "source.md",
            "text/markdown",
            "f" * 64,
            10,
            "available",
            "v1",
            created,
            created,
            None,
        )
    if kind == "messages":
        return identity, "conversation-1", None, "user", "text", "completed", "[]", "{}", created
    return identity, "user-1", "Title", "active", created, created


@pytest.mark.parametrize("kind", ["conversations", "messages", "documents"])
@pytest.mark.parametrize("host_offset", [-5, 0, 8])
def test_each_list_reaches_the_next_page_without_timezone_loss(
    kind: str, host_offset: int, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(LocalDatabaseTime, "host_offset", timedelta(hours=host_offset))
    times = [
        LocalDatabaseTime(2026, 1, 1, 12, 34, 56, 123456),
        LocalDatabaseTime(2026, 1, 1, 12, 34, 56, 123455),
        LocalDatabaseTime(2026, 1, 1, 12, 34, 56, 123454),
    ]
    column = {"conversations": 4, "messages": 8, "documents": 9}[kind]
    pool = PagePool([row_for(kind, str(i), t) for i, t in enumerate(times)], column)
    if kind == "documents":
        method = DocumentRepository(pool).list
        scope = {"user_id": "user-1"}
        id_field = "document_id"
    elif kind == "messages":
        method = ConversationRepository(pool).list_messages
        scope = {"conversation_id": "conversation-1"}
        id_field = "message_id"
    else:
        method = ConversationRepository(pool).list
        scope = {"user_id": "user-1"}
        id_field = "conversation_id"
    first, cursor = method(**scope, limit=1, cursor=None)
    assert cursor == "1767270896123456"
    seen = [getattr(first[0], id_field)]
    for _ in range(2):
        page, cursor = method(**scope, limit=1, cursor=cursor)
        assert len(page) == 1
        seen.append(getattr(page[0], id_field))
    assert seen == ["0", "1", "2"]
    assert cursor is None
    assert pool.boundaries == times[:2]
