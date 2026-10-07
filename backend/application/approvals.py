"""HITL 边界的 Approval 决策（ADR-013 / API-006）。

Approve 会把被中断的 Run 保留为 ``original_run_id``，将其标记为终态
``handoff_complete``，并在同一 Thread 与 Checkpoint 沿革上创建一个 successor Run。
Reject 会以 ``rejected`` 终止原始 Run，且不创建 successor。
决策按 ``approval_request_id`` 幂等，且不会产生第二次副作用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from backend.domain.errors import AppError, authorization_error, conflict, not_found
from backend.infrastructure.repositories.approvals import ApprovalRecord, ApprovalRepository
from backend.infrastructure.repositories.conversation import RunRepository, ThreadRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionResult:
    approval_request_id: str
    approval_lifecycle: Literal["approved", "rejected"]
    run_id: str
    status: str
    successor_run_id: str | None = None
    idempotent_replay: bool = False


class ApprovalService:
    def __init__(
        self,
        *,
        approvals: ApprovalRepository,
        runs: RunRepository,
        threads: ThreadRepository,
        events,
    ) -> None:
        self.approvals = approvals
        self.runs = runs
        self.threads = threads
        self.events = events

    def get(self, approval_request_id: str, *, user_id: str) -> ApprovalRecord:
        approval = self.approvals.get(approval_request_id)
        if approval is None or approval.user_id != user_id:
            raise not_found("Approval request was not found.")
        if approval.status == "pending" and approval.is_expired():
            self.approvals.mark_expired(approval_request_id)
            approval = self.approvals.get(approval_request_id) or approval
        return approval

    def decide(
        self,
        approval_request_id: str,
        *,
        decision: Literal["approve", "reject"],
        user_id: str,
        conversation_id: str,
        thread_id: str,
        run_id: str,
    ) -> DecisionResult:
        approval = self.approvals.get(approval_request_id)
        if approval is None or approval.user_id != user_id:
            raise not_found("Approval request was not found.")
        if (
            approval.conversation_id != conversation_id
            or approval.thread_id != thread_id
            or approval.run_id != run_id
        ):
            raise authorization_error("The approval does not match this run.")

        lifecycle: Literal["approved", "rejected"] = (
            "approved" if decision == "approve" else "rejected"
        )
        if approval.status == lifecycle:
            # 重复的相同决策：返回相同的逻辑结果，不产生第二次副作用。
            return DecisionResult(
                approval_request_id,
                lifecycle,
                run_id,
                "resume_scheduled",
                approval.successor_run_id,
                idempotent_replay=True,
            )
        if approval.status in {"rejected", "expired"}:
            raise conflict("This approval request already reached a terminal decision.")
        if approval.is_expired():
            self.approvals.mark_expired(approval_request_id)
            raise conflict("This approval request has expired.")

        original = self.runs.get(approval.run_id)
        if original is None:
            raise not_found("The interrupted run was not found.")

        if decision == "reject":
            # Reject 不能执行 Tool；不会创建执行 successor。
            if not self.approvals.decide(
                approval_request_id, lifecycle="rejected", decided_by=user_id
            ):
                current = self.approvals.get(approval_request_id)
                if current is None or current.status != "rejected":
                    raise conflict("The approval decision could not be applied.")
            self.runs.update_status(
                original.run_id,
                "rejected",
                error_code="approval_rejected",
                error_message="The export was rejected.",
                terminal=True,
            )
            self.events.publish(
                run_id=original.run_id,
                thread_id=original.thread_id,
                event_type="final",
                payload={
                    "status": "rejected",
                    "content": "The export request was rejected. No export was created.",
                },
            )
            return DecisionResult(
                approval_request_id, "rejected", original.run_id, "resume_scheduled"
            )

        successor = self.runs.create(
            conversation_id=original.conversation_id,
            thread_id=original.thread_id,
            user_id=original.user_id,
            user_input=original.user_input,
            status="accepted",
            predecessor_run_id=original.run_id,
            original_run_id=original.run_id,
            approval_request_id=approval_request_id,
        )
        if not self.approvals.decide(
            approval_request_id,
            lifecycle="approved",
            decided_by=user_id,
            successor_run_id=successor.run_id,
        ):
            current = self.approvals.get(approval_request_id)
            if current is None or current.status != "approved":
                raise conflict("The approval decision could not be applied.")
            if current.successor_run_id:
                successor = self.runs.get(current.successor_run_id) or successor
        # 先发布原始 Run 的终态 final 事件，再让它变为终态，
        # 这样仍附着在等待流上的客户端能观察到 handoff，
        # 而不是看到一条没有终态事件就直接关闭的流。
        self.events.publish(
            run_id=original.run_id,
            thread_id=original.thread_id,
            event_type="final",
            payload={
                "status": "handoff_complete",
                "content": "The export was approved; execution continues in the successor run.",
                "successor_run_id": successor.run_id,
            },
        )
        self.runs.update_status(
            original.run_id,
            "handoff_complete",
            terminal=True,
        )
        return DecisionResult(
            approval_request_id, "approved", original.run_id, "resume_scheduled", successor.run_id
        )

    def expire_due(self, *, limit: int = 100) -> list[str]:
        expired = self.approvals.expire_due(limit=limit)
        for approval_request_id in expired:
            approval = self.approvals.get(approval_request_id)
            if approval is None:
                continue
            run = self.runs.get(approval.run_id)
            if run is None or run.status != "awaiting_approval":
                continue
            self.runs.update_status(
                run.run_id,
                "expired",
                error_code="approval_expired",
                error_message="The approval request expired without a decision.",
                terminal=True,
            )
            self.events.publish(
                run_id=run.run_id,
                thread_id=run.thread_id,
                event_type="final",
                payload={
                    "status": "expired",
                    "content": "The approval request expired. No export was created.",
                },
            )
        return expired


__all__ = ["ApprovalService", "AppError", "DecisionResult"]
