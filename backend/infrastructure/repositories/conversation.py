"""Conversation、thread、message、run 与 event 的持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from backend.domain.errors import ExecutionOwnershipLost
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
    # DATETIME(6) 列使用「朴素 UTC」约定（见 domain.identifiers.utc_now），因此这里用
    # timezone-aware 的 fromtimestamp 构造后显式去除 tzinfo：
    # 语义与已弃用的 datetime.utcfromtimestamp 完全一致，且不触发弃用 API。
    return datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC).replace(tzinfo=None)


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
        with self.pool.cursor() as cursor:
            self.touch_with_cursor(cursor, conversation_id)

    def touch_with_cursor(self, cursor, conversation_id: str) -> None:
        """在调用方事务内刷新会话时间戳（与终态转换共享事务）。"""

        cursor.execute(
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
        with self.pool.cursor() as cursor:
            return self.add_message_with_cursor(
                cursor,
                conversation_id=conversation_id,
                role=role,
                content=content,
                status=status,
                run_id=run_id,
                citations=citations,
                metadata=metadata,
            )

    def add_message_with_cursor(
        self,
        cursor,
        *,
        conversation_id: str,
        role: str,
        content: str,
        status: str,
        run_id: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MessageRecord:
        """在调用方事务内写入一条会话消息。

        终态回答必须与终态转换同事务：只有赢得终态 CAS 的执行者才写入 assistant 消息，
        输掉竞争的陈旧执行器不会产生重复消息（STATE：「Conversation Persistence 与
        Application Event 分离」）。
        """

        message_id = new_id()
        now = utc_now()
        cursor.execute(
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
        run_id: str | None = None,
    ) -> RunRecord:
        with self.pool.cursor() as cursor:
            return insert_run_row(
                cursor,
                run_id=run_id or new_id(),
                conversation_id=conversation_id,
                thread_id=thread_id,
                user_id=user_id,
                user_input=user_input,
                status=status,
                predecessor_run_id=predecessor_run_id,
                original_run_id=original_run_id,
                approval_request_id=approval_request_id,
                client_metadata=client_metadata,
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

    def claim_for_execution(self, run_id: str, *, owner_token: str) -> bool:
        """把 ``accepted`` Run 原子地转换为 ``running`` 并记录执行所有权。

        一个 Run 最多被一个执行器领取：重复 dispatch、重复 approval、已终态或正在执行的
        Run 都不能再次执行 Graph（HITL/Resume 幂等边界）。``owner_token`` 是本次执行的
        栅栏标识：领取之后的所有提交点都必须携带它（ADR-035）。
        """

        with self.pool.transaction() as cursor:
            return self.claim_for_execution_with_cursor(cursor, run_id, owner_token=owner_token)

    def claim_for_execution_with_cursor(self, cursor, run_id: str, *, owner_token: str) -> bool:
        """在调用方事务内领取 ``accepted`` Run。

        ``RunService`` 使用这个变体与首个 ``agent_start`` 事件共享事务，
        使恢复预算不会漏计「领取成功后、事件发布前」的进程崩溃窗口。
        """

        now = utc_now()
        affected = cursor.execute(
            "UPDATE runs SET status = 'running', owner_token = %s, heartbeat_at = %s, "
            "updated_at = %s WHERE run_id = %s AND status = 'accepted'",
            (owner_token[:64], now, now, run_id),
        )
        return int(affected) > 0

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
        with self.pool.cursor() as cursor:
            self.update_status_with_cursor(
                cursor,
                run_id,
                status,
                routing_outcome=routing_outcome,
                intent=intent,
                final_response=final_response,
                error_code=error_code,
                error_message=error_message,
                approval_request_id=approval_request_id,
                query_result_id=query_result_id,
                terminal=terminal,
            )

    def update_status_with_cursor(
        self,
        cursor,
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
        required_status: str | None = None,
        required_owner: str | None = None,
    ) -> bool:
        """在调用方事务内更新 Run 状态；给出 ``required_status`` 时是条件更新（CAS）。

        终态转换需要与它的终态事件共享同一个事务（见 ``application.terminal``），
        因此状态写入必须能在调用方事务的游标上执行。

        ``required_owner`` 追加执行所有权条件（ADR-035）：只有仍持有本次执行 token 的
        执行者才能写入等待态与终态——陈旧执行器即使在 ``release_stale_claim`` 之后被
        重新调度（状态又变回 ``running``）也赢不了 CAS。写入非执行态（终态、
        ``awaiting_approval``）时清空 token，让栅栏随执行结束一起失效。
        """

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
        if terminal or status != "running":
            assignments.append("owner_token = NULL")
        parameters.append(run_id)
        clause = ""
        if required_status is not None:
            clause += " AND status = %s"
            parameters.append(required_status)
        if required_owner is not None:
            clause += " AND owner_token = %s"
            parameters.append(required_owner)
        affected = cursor.execute(
            f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = %s{clause}", parameters
        )
        return int(affected) > 0

    def list_stale_running_successors(self, *, older_than: datetime, limit: int = 20) -> list[str]:
        """带审批绑定、且租约已陈旧的 ``running`` successor Run。

        这是进程在 Graph 执行中途退出（或执行器崩溃）后遗留的候选集合；
        只有 HITL successor 参与「释放 + 重新调度」恢复，普通 Run 走安全终态
        （``list_stale_running_ordinary``，见 ADR-035）。

        陈旧判定基于 ``COALESCE(heartbeat_at, updated_at)``：由独立心跳线程续租的
        ``heartbeat_at`` 是租约权威时间戳，未迁移的旧行退回 ``updated_at``。
        """

        rows = self.pool.query(
            "SELECT run_id FROM runs WHERE status = 'running' "
            "AND approval_request_id IS NOT NULL AND original_run_id IS NOT NULL "
            "AND COALESCE(heartbeat_at, updated_at) < %s ORDER BY updated_at ASC LIMIT %s",
            (older_than, limit),
        )
        return [str(row[0]) for row in rows]

    def list_stale_running_ordinary(self, *, older_than: datetime, limit: int = 20) -> list[str]:
        """没有审批绑定的 ``running`` Run（普通 Run）中租约已陈旧的候选集合。

        普通 Run 没有可用的「已批准续跑」入口，重新调度会把整个 Graph 再执行一遍，
        因此它们不会被释放重跑，而是被安全终止（``execution_interrupted``）。
        """

        rows = self.pool.query(
            "SELECT run_id FROM runs WHERE status = 'running' "
            "AND approval_request_id IS NULL AND original_run_id IS NULL "
            "AND COALESCE(heartbeat_at, updated_at) < %s ORDER BY updated_at ASC LIMIT %s",
            (older_than, limit),
        )
        return [str(row[0]) for row in rows]

    def release_stale_claim(self, run_id: str, *, older_than: datetime) -> bool:
        """把陈旧的 ``running`` successor 原子地转回 ``accepted``（可重新调度）。

        双重条件（状态 + 陈旧时间）保证：只有确实超过陈旧阈值的执行声明才会被释放，
        仍在正常执行（独立心跳持续刷新 ``heartbeat_at``）的 Run 不会被误释放。
        释放同时清空 ``owner_token``：被释放的执行者随之失去所有权，不能再写事件、
        等待态或终态（ADR-035 的栅栏语义）。
        """

        affected = self.pool.execute(
            "UPDATE runs SET status = 'accepted', owner_token = NULL, updated_at = %s "
            "WHERE run_id = %s AND status = 'running' "
            "AND COALESCE(heartbeat_at, updated_at) < %s",
            (utc_now(), run_id, older_than),
        )
        return affected > 0

    def heartbeat(self, run_id: str, *, owner_token: str) -> bool:
        """执行期续租：仅当 Run 仍是 ``running`` 且仍由该 token 持有时推进租约。

        心跳把「最近一次可见执行活动」写到 Run 行上，使维护任务的陈旧判定基于
        执行者的活跃证据，而不是仅凭领取时刻；终态 Run 不会被心跳复活，
        已失去所有权的执行者也续不了租（返回 ``False``）。
        """

        now = utc_now()
        affected = self.pool.execute(
            "UPDATE runs SET heartbeat_at = %s, updated_at = %s "
            "WHERE run_id = %s AND status = 'running' AND owner_token = %s",
            (now, now, run_id, owner_token),
        )
        return affected > 0

    def is_owned_by(self, run_id: str, *, owner_token: str) -> bool:
        """该 Run 是否仍处于 ``running`` 且仍归属于该执行 token（提交点校验用）。"""

        row = self.pool.query_one(
            "SELECT 1 FROM runs WHERE run_id = %s AND status = 'running' AND owner_token = %s",
            (run_id, owner_token),
        )
        return row is not None

    def list_for_conversation(self, conversation_id: str, *, limit: int = 20) -> list[RunRecord]:
        rows = self.pool.query(
            f"SELECT {RUN_COLUMNS} FROM runs "
            "WHERE conversation_id = %s ORDER BY created_at DESC LIMIT %s",
            (conversation_id, limit),
        )
        return [record for row in rows if (record := _run(row)) is not None]


def reserve_event_with_cursor(
    cursor,
    *,
    run_id: str,
    event_id: str,
    event_type: str,
    thread_id: str,
    payload: dict[str, Any],
    created_at: datetime | None = None,
    required_owner: str | None = None,
) -> int:
    """在调用方事务内预留该 Run 的下一个 sequence 并写入事件，返回 sequence。

    幂等：``run_events`` 对 ``event_id`` 与 ``(run_id, sequence)`` 都有唯一键，
    同一 ``event_id`` 重复写入时直接返回既有 sequence，不会产生第二条事件。

    ``required_owner`` 给出时是执行期提交点校验（ADR-035）：锁住 Run 行后确认它仍是
    本次执行的 ``running`` 且仍由该 token 持有，否则抛 ``ExecutionOwnershipLost`` 且
    不写入任何事件行——失去所有权的执行者不得再向该 Run 的事件流追加内容。
    """

    cursor.execute(
        "SELECT sequence FROM run_events WHERE run_id = %s AND event_id = %s",
        (run_id, event_id),
    )
    existing = cursor.fetchone()
    if existing is not None:
        return int(existing[0])
    # 锁住 Run 行，使并发发布者绝不会被分配到相同的 sequence。
    cursor.execute("SELECT status, owner_token FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
    run_row = cursor.fetchone()
    if run_row is None:
        raise KeyError(run_id)
    if required_owner is not None and (
        str(run_row[0]) != "running" or run_row[1] != required_owner
    ):
        raise ExecutionOwnershipLost(run_id)
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
    return sequence


def insert_run_row(
    cursor,
    *,
    run_id: str,
    conversation_id: str,
    thread_id: str,
    user_id: str,
    user_input: str,
    status: str,
    predecessor_run_id: str | None = None,
    original_run_id: str | None = None,
    approval_request_id: str | None = None,
    client_metadata: dict[str, Any] | None = None,
) -> RunRecord:
    """在给定游标上插入一条 Run（供普通创建与 HITL 事务共用同一列定义）。"""

    now = utc_now()
    cursor.execute(
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
        required_owner: str | None = None,
    ) -> int:
        """以原子方式预留该 Run 的下一个 sequence 并持久化事件。

        全程锁住 Run 行，因此并发的发布者（例如前驱 Run 尚未收尾时启动的后继 Run）
        绝不会被分配到相同的 ``sequence``。``required_owner`` 是执行期提交点校验
        （ADR-035）：失去所有权的执行者拿不到 sequence，也写不进事件。
        """

        with self.pool.transaction() as cursor:
            return reserve_event_with_cursor(
                cursor,
                run_id=run_id,
                event_id=event_id,
                event_type=event_type,
                thread_id=thread_id,
                payload=payload,
                created_at=created_at,
                required_owner=required_owner,
            )

    def reserve_with_cursor(
        self,
        cursor,
        *,
        run_id: str,
        event_id: str,
        event_type: str,
        thread_id: str,
        payload: dict[str, Any],
        created_at: datetime | None = None,
        required_owner: str | None = None,
    ) -> int:
        """在调用方事务内预留 sequence 并写入事件（供终态事件的原子写入使用）。"""

        return reserve_event_with_cursor(
            cursor,
            run_id=run_id,
            event_id=event_id,
            event_type=event_type,
            thread_id=thread_id,
            payload=payload,
            created_at=created_at,
            required_owner=required_owner,
        )

    def count_of_type(self, *, run_id: str, event_type: str) -> int:
        """该 Run 已持久化的某类事件数量（判定执行尝试次数等有界恢复条件）。"""

        row = self.pool.query_one(
            "SELECT COUNT(*) FROM run_events WHERE run_id = %s AND event_type = %s",
            (run_id, event_type),
        )
        return int(row[0]) if row else 0

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
