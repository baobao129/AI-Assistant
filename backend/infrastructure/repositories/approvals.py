"""HITL 边界的 Approval Request 持久化（ADR-013）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool

ApprovalLifecycle = Literal["pending", "approved", "rejected", "expired"]

TERMINAL_LIFECYCLES: frozenset[str] = frozenset({"approved", "rejected", "expired"})

APPROVAL_COLUMNS = (
    "approval_request_id, run_id, thread_id, conversation_id, user_id, status, summary_json, "
    "binding_json, created_at, expires_at, decided_at, decided_by, successor_run_id, artifact_id, "
    "side_effect_completed_at"
)


@dataclass(frozen=True)
class ApprovalRecord:
    approval_request_id: str
    run_id: str
    thread_id: str
    conversation_id: str
    user_id: str
    status: str
    summary: dict[str, Any]
    binding: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    successor_run_id: str | None
    artifact_id: str | None
    side_effect_completed_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_LIFECYCLES

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


class ApprovalRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        conversation_id: str,
        user_id: str,
        summary: dict[str, Any],
        binding: dict[str, Any],
        expires_at: datetime,
    ) -> ApprovalRecord:
        approval_request_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO approvals (approval_request_id, run_id, thread_id, conversation_id, "
            "user_id, status, summary_json, binding_json, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)",
            (
                approval_request_id,
                run_id,
                thread_id,
                conversation_id,
                user_id,
                json.dumps(summary, ensure_ascii=False),
                json.dumps(binding, ensure_ascii=False),
                now,
                expires_at,
            ),
        )
        return ApprovalRecord(
            approval_request_id=approval_request_id,
            run_id=run_id,
            thread_id=thread_id,
            conversation_id=conversation_id,
            user_id=user_id,
            status="pending",
            summary=summary,
            binding=binding,
            created_at=now,
            expires_at=expires_at,
            decided_at=None,
            decided_by=None,
            successor_run_id=None,
            artifact_id=None,
            side_effect_completed_at=None,
        )

    def get(self, approval_request_id: str) -> ApprovalRecord | None:
        row = self.pool.query_one(
            f"SELECT {APPROVAL_COLUMNS} FROM approvals WHERE approval_request_id = %s",
            (approval_request_id,),
        )
        return _approval(row)

    def list_for_conversation(self, conversation_id: str) -> list[ApprovalRecord]:
        rows = self.pool.query(
            f"SELECT {APPROVAL_COLUMNS} FROM approvals "
            "WHERE conversation_id = %s ORDER BY created_at DESC",
            (conversation_id,),
        )
        return [record for row in rows if (record := _approval(row)) is not None]

    def decide(
        self,
        approval_request_id: str,
        *,
        lifecycle: Literal["approved", "rejected"],
        decided_by: str,
        successor_run_id: str | None = None,
    ) -> bool:
        """从 ``pending`` 到终态决策的幂等转换。

        本次调用完成了转换时返回 ``True``；重复决策返回 ``False``，且绝不产生第二次副作用。
        """

        with self.pool.cursor() as cursor:
            return decide_with_cursor(
                cursor,
                approval_request_id,
                lifecycle=lifecycle,
                decided_by=decided_by,
                successor_run_id=successor_run_id,
            )

    def list_pending_successor_run_ids(self, *, limit: int = 20) -> list[tuple[str, str]]:
        """已批准但其 successor Run 从未被领取执行的 ``(run_id, approval_request_id)``。

        这是崩溃或调度容量耗尽后的**持久化调度意图**：只要该行存在，恢复任务
        （或任何幂等重试）都能重新调度它，而不会创建第二个 successor。
        """

        rows = self.pool.query(
            "SELECT r.run_id, a.approval_request_id FROM approvals a "
            "JOIN runs r ON r.run_id = a.successor_run_id "
            "WHERE a.status = 'approved' AND r.status = 'accepted' "
            "ORDER BY a.decided_at LIMIT %s",
            (limit,),
        )
        return [(str(row[0]), str(row[1])) for row in rows]

    def mark_expired(self, approval_request_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE approvals SET status = 'expired', decided_at = %s "
            "WHERE approval_request_id = %s AND status = 'pending'",
            (utc_now(), approval_request_id),
        )
        return affected > 0

    def list_due_pending(self, *, limit: int = 100) -> list[ApprovalRecord]:
        """已到期但仍待决策的审批请求（过期转换的候选集合，供维护任务有界处理）。

        只按 ``expires_at`` 与 ``pending`` 生命周期筛选；到期的审批在转换前始终留在
        该集合里，因此任何中断都能被下一次维护周期重新拾起，不会因为「已改为 expired」
        而漏掉尚未终结的 Run。
        """

        rows = self.pool.query(
            f"SELECT {APPROVAL_COLUMNS} FROM approvals "
            "WHERE status = 'pending' AND expires_at <= %s "
            "ORDER BY expires_at ASC LIMIT %s",
            (utc_now(), limit),
        )
        return [record for row in rows if (record := _approval(row)) is not None]

    def list_expired_unterminated(self, *, limit: int = 20) -> list[str]:
        """已过期、但其原 Run 仍停在 ``awaiting_approval`` 的审批（缺口修复候选）。

        这类行只会由「先提交审批 expired、再更新 Run」的非原子路径留下（历史版本或
        异常中断）。它们已不是 ``pending``，因此不会进入常规过期集合；本查询让维护任务
        仍能把对应 Run 补齐为终态，避免残留永久存在。
        """

        rows = self.pool.query(
            "SELECT a.approval_request_id FROM approvals a "
            "JOIN runs r ON r.run_id = a.run_id "
            "WHERE a.status = 'expired' AND r.status = 'awaiting_approval' "
            "ORDER BY a.decided_at LIMIT %s",
            (limit,),
        )
        return [str(row[0]) for row in rows]

    def record_artifact(self, approval_request_id: str, *, artifact_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE approvals SET artifact_id = %s, side_effect_completed_at = %s "
            "WHERE approval_request_id = %s AND status = 'approved' AND artifact_id IS NULL",
            (artifact_id, utc_now(), approval_request_id),
        )
        return affected > 0


def decide_with_cursor(
    cursor,
    approval_request_id: str,
    *,
    lifecycle: Literal["approved", "rejected"],
    decided_by: str,
    successor_run_id: str | None = None,
) -> bool:
    """在调用方的事务内执行 ``pending`` 到终态决策的条件更新（幂等 CAS）。

    CAS 同时校验 ``expires_at > NOW``：过期判定以数据库时间为权威，应用层的
    ``is_expired()`` 预检查与本次更新之间即便发生时间竞态，过期审批也绝不会被批准。
    """

    now = utc_now()
    affected = cursor.execute(
        "UPDATE approvals SET status = %s, decided_at = %s, decided_by = %s, "
        "successor_run_id = %s WHERE approval_request_id = %s AND status = 'pending' "
        "AND expires_at > %s",
        (lifecycle, now, decided_by, successor_run_id, approval_request_id, now),
    )
    return int(affected) > 0


def expire_with_cursor(cursor, approval_request_id: str) -> bool:
    """在调用方的事务内把待决策审批条件转换为 ``expired``（幂等 CAS）。"""

    affected = cursor.execute(
        "UPDATE approvals SET status = 'expired', decided_at = %s "
        "WHERE approval_request_id = %s AND status = 'pending'",
        (utc_now(), approval_request_id),
    )
    return int(affected) > 0


def _approval(row: tuple[Any, ...] | None) -> ApprovalRecord | None:
    if row is None:
        return None
    return ApprovalRecord(
        approval_request_id=str(row[0]),
        run_id=str(row[1]),
        thread_id=str(row[2]),
        conversation_id=str(row[3]),
        user_id=str(row[4]),
        status=str(row[5]),
        summary=json.loads(row[6]) if row[6] else {},
        binding=json.loads(row[7]) if row[7] else {},
        created_at=row[8],
        expires_at=row[9],
        decided_at=row[10],
        decided_by=row[11],
        successor_run_id=row[12],
        artifact_id=row[13],
        side_effect_completed_at=row[14],
    )
