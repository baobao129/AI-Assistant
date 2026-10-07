"""Conversation、thread、message、run 与 event 的持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool

RunStatus = Literal[
    "accepted", "running", "awaiting_approval", "completed", "rejected", "failed", "expired"
]

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"completed", "rejected", "failed", "expired", "handoff_complete"}
)

NON_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"accepted", "running", "awaiting_approval"})

CONVERSATION_COLUMNS = "conversation_id, user_id, title, status, created_at, updated_at"
MESSAGE_COLUMNS = (
    "message_id, conversation_id, run_id, role, content, status, citations_json, "
    "metadata_json, created_at"
)
RUN_COLUMNS = (
    "run_id, conversation_id, thread_id, user_id, user_input, status, routing_outcome, intent, "
    "final_response, error_code, error_message, approval_request_id, original_run_id, "
    "predecessor_run_id, query_result_id, created_at, updated_at, completed_at"
)
EVENT_COLUMNS = "event_id, sequence, event_type, thread_id, payload_json, created_at"


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    user_id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MessageRecord:
    message_id: str
    conversation_id: str
    run_id: str | None
    role: str
    content: str
    status: str
    citations: list[dict[str, Any]]
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    conversation_id: str
    thread_id: str
    user_id: str
    user_input: str
    status: str
    routing_outcome: str | None
    intent: str | None
    final_response: str | None
    error_code: str | None
    error_message: str | None
    approval_request_id: str | None
    original_run_id: str | None
    predecessor_run_id: str | None
    query_result_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


def _cursor_from(value: datetime) -> str:
    # 分页 cursor 编码为微秒级时间戳字符串：与 SQL 的排序键一致，且不暴露内部 ID。
    return f"{int(value.timestamp() * 1_000_000)}"


def _cursor_to_datetime(cursor: str) -> datetime:
    microseconds = int(cursor)
    return datetime.utcfromtimestamp(microseconds / 1_000_000)


class ConversationRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(self, *, user_id: str, title: str) -> ConversationRecord:
        conversation_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO conversations (conversation_id, user_id, title, status, created_at, "
            "updated_at) VALUES (%s, %s, %s, 'active', %s, %s)",
            (conversation_id, user_id, title[:200], now, now),
        )
        return ConversationRecord(conversation_id, user_id, title[:200], "active", now, now)

    def get(self, conversation_id: str, *, user_id: str) -> ConversationRecord | None:
        row = self.pool.query_one(
            "SELECT conversation_id, user_id, title, status, created_at, updated_at "
            "FROM conversations WHERE conversation_id = %s AND user_id = %s AND status = 'active'",
            (conversation_id, user_id),
        )
        return _conversation(row)

    def list(
        self, *, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationRecord], str | None]:
        parameters: list[Any] = [user_id]
        clause = ""
        if cursor:
            clause = "AND created_at < %s"
            parameters.append(_cursor_to_datetime(cursor))
        parameters.append(limit + 1)
        rows = self.pool.query(
            "SELECT conversation_id, user_id, title, status, created_at, updated_at "
            "FROM conversations WHERE user_id = %s AND status = 'active' "
            f"{clause} ORDER BY created_at DESC, conversation_id DESC LIMIT %s",
            parameters,
        )
        items = [record for row in rows[:limit] if (record := _conversation(row)) is not None]
        next_cursor = _cursor_from(items[-1].created_at) if len(rows) > limit and items else None
        return items, next_cursor

    def archive(self, conversation_id: str, *, user_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE conversations SET status = 'archived', deleted_at = %s, updated_at = %s "
            "WHERE conversation_id = %s AND user_id = %s AND status = 'active'",
            (utc_now(), utc_now(), conversation_id, user_id),
        )
        return affected > 0

    def touch(self, conversation_id: str) -> None:
        self.pool.execute(
            "UPDATE conversations SET updated_at = %s WHERE conversation_id = %s",
            (utc_now(), conversation_id),
        )

    # Message --------------------------------------------------------------
    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        status: str,
        run_id: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MessageRecord:
        message_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, run_id, role, "
            "content, "
            "status, citations_json, metadata_json, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                message_id,
                conversation_id,
                run_id,
                role,
                content,
                status,
                json.dumps(citations or [], ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
            ),
        )
        return MessageRecord(
            message_id,
            conversation_id,
            run_id,
            role,
            content,
            status,
            citations or [],
            metadata or {},
            now,
        )

    def list_messages(
        self, *, conversation_id: str, limit: int, cursor: str | None
    ) -> tuple[list[MessageRecord], str | None]:
        parameters: list[Any] = [conversation_id]
        clause = ""
        if cursor:
            clause = "AND created_at < %s"
            parameters.append(_cursor_to_datetime(cursor))
        parameters.append(limit + 1)
        rows = self.pool.query(
            f"SELECT {MESSAGE_COLUMNS} FROM conversation_messages WHERE conversation_id = %s "
            f"{clause} ORDER BY created_at DESC, message_id DESC LIMIT %s",
            parameters,
        )
        items = [_message(row) for row in rows[:limit]]
        next_cursor = _cursor_from(items[-1].created_at) if len(rows) > limit and items else None
        return items, next_cursor


class ThreadRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(self, *, conversation_id: str) -> str:
        thread_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO threads (thread_id, conversation_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s)",
            (thread_id, conversation_id, now, now),
        )
        return thread_id

    def belongs_to_conversation(self, *, thread_id: str, conversation_id: str) -> bool:
        row = self.pool.query_one(
            "SELECT 1 FROM threads WHERE thread_id = %s AND conversation_id = %s",
            (thread_id, conversation_id),
        )
        return row is not None


class RunRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        conversation_id: str,
        thread_id: str,
        user_id: str,
        user_input: str,
        status: str = "accepted",
        predecessor_run_id: str | None = None,
        original_run_id: str | None = None,
        approval_request_id: str | None = None,
        client_metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        run_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO runs (run_id, conversation_id, thread_id, user_id, user_input, status, "
            "approval_request_id, original_run_id, predecessor_run_id, client_metadata_json, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                conversation_id,
                thread_id,
                user_id,
                user_input,
                status,
                approval_request_id,
                original_run_id,
                predecessor_run_id,
                json.dumps(client_metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        return RunRecord(
            run_id=run_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            user_id=user_id,
            user_input=user_input,
            status=status,
            routing_outcome=None,
            intent=None,
            final_response=None,
            error_code=None,
            error_message=None,
            approval_request_id=approval_request_id,
            original_run_id=original_run_id,
            predecessor_run_id=predecessor_run_id,
            query_result_id=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )

    def get(self, run_id: str) -> RunRecord | None:
        row = self.pool.query_one(
            f"SELECT {RUN_COLUMNS} FROM runs WHERE run_id = %s",
            (run_id,),
        )
        return _run(row)

    def find_active_run_for_thread(self, thread_id: str) -> RunRecord | None:
        placeholders = ", ".join(["%s"] * len(NON_TERMINAL_RUN_STATUSES))
        row = self.pool.query_one(
            f"SELECT {RUN_COLUMNS} FROM runs "
            f"WHERE thread_id = %s AND status IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (thread_id, *sorted(NON_TERMINAL_RUN_STATUSES)),
        )
        return _run(row)

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        routing_outcome: str | None = None,
        intent: str | None = None,
        final_response: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        approval_request_id: str | None = None,
        query_result_id: str | None = None,
        terminal: bool = False,
    ) -> None:
        now = utc_now()
        assignments = ["status = %s", "updated_at = %s"]
        parameters: list[Any] = [status, now]
        for column, value in (
            ("routing_outcome", routing_outcome),
            ("intent", intent),
            ("final_response", final_response),
            ("error_code", error_code),
            ("error_message", error_message),
            ("approval_request_id", approval_request_id),
            ("query_result_id", query_result_id),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        if terminal:
            assignments.append("completed_at = %s")
            parameters.append(now)
        parameters.append(run_id)
        self.pool.execute(f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = %s", parameters)

    def list_for_conversation(self, conversation_id: str, *, limit: int = 20) -> list[RunRecord]:
        rows = self.pool.query(
            f"SELECT {RUN_COLUMNS} FROM runs "
            "WHERE conversation_id = %s ORDER BY created_at DESC LIMIT %s",
            (conversation_id, limit),
        )
        return [record for row in rows if (record := _run(row)) is not None]


class RunEventRepository:
    """SSE 事件的 replay 存储；Conversation Persistence 不是 replay 存储。"""

    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def append(
        self,
        *,
        run_id: str,
        event_id: str,
        event_type: str,
        thread_id: str,
        payload: dict[str, Any],
        created_at: datetime | None = None,
    ) -> int:
        """以原子方式预留该 Run 的下一个 sequence 并持久化事件。

        全程锁住 Run 行，因此并发的发布者（例如前驱 Run 尚未收尾时启动的后继 Run）
        绝不会被分配到相同的 ``sequence``。
        """

        with self.pool.connection() as connection:
            connection.begin()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT run_id FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
                    )
                    if cursor.fetchone() is None:
                        raise KeyError(run_id)
                    cursor.execute(
                        "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = %s",
                        (run_id,),
                    )
                    row = cursor.fetchone()
                    sequence = (int(row[0]) if row else 0) + 1
                    cursor.execute(
                        "INSERT INTO run_events (run_id, sequence, event_id, event_type, "
                        "thread_id, payload_json, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            run_id,
                            sequence,
                            event_id,
                            event_type,
                            thread_id,
                            json.dumps(payload, ensure_ascii=False),
                            created_at or utc_now(),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return sequence

    def list_after(
        self, *, run_id: str, after_event_id: str | None
    ) -> list[tuple[str, int, str, str, dict[str, Any], datetime]]:
        start_sequence = 0
        if after_event_id:
            row = self.pool.query_one(
                "SELECT sequence FROM run_events WHERE run_id = %s AND event_id = %s",
                (run_id, after_event_id),
            )
            if row is None:
                raise KeyError(after_event_id)
            start_sequence = int(row[0])
        rows = self.pool.query(
            f"SELECT {EVENT_COLUMNS} FROM run_events "
            "WHERE run_id = %s AND sequence > %s ORDER BY sequence ASC",
            (run_id, start_sequence),
        )
        return [
            (str(row[0]), int(row[1]), str(row[2]), str(row[3]), json.loads(row[4]), row[5])
            for row in rows
        ]

    def max_sequence(self, run_id: str) -> int:
        row = self.pool.query_one(
            "SELECT MAX(sequence) FROM run_events WHERE run_id = %s", (run_id,)
        )
        return int(row[0]) if row and row[0] is not None else 0

    def trim(self, *, run_id: str, keep: int) -> int:
        """每个 run 仅保留最新的 ``keep`` 条事件；超出范围的 replay 返回 409。"""

        row = self.pool.query_one(
            "SELECT sequence FROM run_events WHERE run_id = %s "
            "ORDER BY sequence DESC LIMIT 1 OFFSET %s",
            (run_id, keep),
        )
        if row is None:
            return 0
        return self.pool.execute(
            "DELETE FROM run_events WHERE run_id = %s AND sequence <= %s", (run_id, int(row[0]))
        )


def _conversation(row: tuple[Any, ...] | None) -> ConversationRecord | None:
    if row is None:
        return None
    return ConversationRecord(
        conversation_id=str(row[0]),
        user_id=str(row[1]),
        title=str(row[2]),
        status=str(row[3]),
        created_at=row[4],
        updated_at=row[5],
    )


def _message(row: tuple[Any, ...]) -> MessageRecord:
    return MessageRecord(
        message_id=str(row[0]),
        conversation_id=str(row[1]),
        run_id=str(row[2]) if row[2] else None,
        role=str(row[3]),
        content=str(row[4]),
        status=str(row[5]),
        citations=json.loads(row[6]) if row[6] else [],
        metadata=json.loads(row[7]) if row[7] else {},
        created_at=row[8],
    )


def _run(row: tuple[Any, ...] | None) -> RunRecord | None:
    if row is None:
        return None
    return RunRecord(
        run_id=str(row[0]),
        conversation_id=str(row[1]),
        thread_id=str(row[2]),
        user_id=str(row[3]),
        user_input=str(row[4]),
        status=str(row[5]),
        routing_outcome=row[6],
        intent=row[7],
        final_response=row[8],
        error_code=row[9],
        error_message=row[10],
        approval_request_id=row[11],
        original_run_id=row[12],
        predecessor_run_id=row[13],
        query_result_id=row[14],
        created_at=row[15],
        updated_at=row[16],
        completed_at=row[17],
    )


def retention_deadline(seconds: int) -> datetime:
    return utc_now() + timedelta(seconds=seconds)
