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

        affected = self.pool.execute(
            "UPDATE approvals SET status = %s, decided_at = %s, decided_by = %s, "
            "successor_run_id = %s WHERE approval_request_id = %s AND status = 'pending'",
            (lifecycle, utc_now(), decided_by, successor_run_id, approval_request_id),
        )
        return affected > 0

    def mark_expired(self, approval_request_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE approvals SET status = 'expired', decided_at = %s "
            "WHERE approval_request_id = %s AND status = 'pending'",
            (utc_now(), approval_request_id),
        )
        return affected > 0

    def expire_due(self, *, limit: int = 100) -> list[str]:
        rows = self.pool.query(
            "SELECT approval_request_id FROM approvals "
            "WHERE status = 'pending' AND expires_at <= %s "
            "ORDER BY expires_at ASC LIMIT %s",
            (utc_now(), limit),
        )
        expired: list[str] = []
        for row in rows:
            if self.mark_expired(str(row[0])):
                expired.append(str(row[0]))
        return expired

    def record_artifact(self, approval_request_id: str, *, artifact_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE approvals SET artifact_id = %s, side_effect_completed_at = %s "
            "WHERE approval_request_id = %s AND status = 'approved' AND artifact_id IS NULL",
            (artifact_id, utc_now(), approval_request_id),
        )
        return affected > 0


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
