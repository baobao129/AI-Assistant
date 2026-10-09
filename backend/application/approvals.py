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

from backend.application.events import EventEnvelope
from backend.application.terminal import (
    TerminalTransition,
    apply_terminal_transition,
    record_terminal_transition,
)
from backend.domain.errors import AppError, authorization_error, conflict, not_found
from backend.domain.identifiers import new_id
from backend.infrastructure.database import MySQLConnectionPool
from backend.infrastructure.repositories.approvals import (
    ApprovalRecord,
    ApprovalRepository,
    decide_with_cursor,
    expire_with_cursor,
)
from backend.infrastructure.repositories.conversation import (
    RunRepository,
    ThreadRepository,
    insert_run_row,
)

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
        pool: MySQLConnectionPool,
    ) -> None:
        self.approvals = approvals
        self.runs = runs
        self.threads = threads
        self.events = events
        # HITL 决策的事务边界（Unit of Work）：决策 CAS、successor 创建与原始 Run 终态
        # 必须在同一个事务内提交，保证不存在「approved 但没有 successor」这类
        # 不可恢复中间态，也不依赖内存锁或前端不重复点击。
        self.pool = pool

    def get(self, approval_request_id: str, *, user_id: str) -> ApprovalRecord:
        approval = self.approvals.get(approval_request_id)
        if approval is None or approval.user_id != user_id:
            raise not_found("Approval request was not found.")
        if approval.status == "pending" and approval.is_expired():
            # 过期是终态转换：审批 CAS、原 Run 终态与关闭事件在同一事务内提交
            # （见 expire_one），绝不留下「审批已 expired 但 Run 仍 awaiting_approval」
            # 这种维护任务再也查不到的中间态。
            self.expire_one(approval_request_id)
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
            # 过期优先于决策：先在单个事务内完成「审批 expired + Run expired + 关闭事件」，
            # 再以 conflict 拒绝本次决策（决策 CAS 同时校验 expires_at，双保险）。
            self.expire_one(approval_request_id)
            raise conflict("This approval request has expired.")

        original = self.runs.get(approval.run_id)
        if original is None:
            raise not_found("The interrupted run was not found.")

        if decision == "reject":
            # Reject 不能执行 Tool；不会创建执行 successor。审批 CAS、原始 Run 的终态
            # 与其终态事件在同一事务内提交：重复/并发 Reject 不会产生第二个 final，
            # 且提交后进程退出也不会丢失关闭事件（见 application.terminal）。
            terminal: EventEnvelope | None = None
            with self.pool.transaction() as cursor:
                applied = decide_with_cursor(
                    cursor, approval_request_id, lifecycle="rejected", decided_by=user_id
                )
                if applied:
                    terminal = apply_terminal_transition(
                        cursor,
                        runs=self.runs,
                        events=self.events,
                        transition=TerminalTransition(
                            run_id=original.run_id,
                            thread_id=original.thread_id,
                            status="rejected",
                            event_type="final",
                            payload={
                                "status": "rejected",
                                "content": (
                                    "The export request was rejected. No export was created."
                                ),
                            },
                            logical_transition="approval_rejected",
                            required_status="awaiting_approval",
                            error_code="approval_rejected",
                            error_message="The export was rejected.",
                        ),
                    )
                    if terminal is None:
                        # 审批 CAS 已成功但原 Run 已离开等待态时，必须让事务回滚；
                        # 否则会提交 rejected 审批而没有对应 Run 终态。
                        raise conflict(
                            "The approval decision could not be applied to the waiting run."
                        )
            if not applied:
                current = self.approvals.get(approval_request_id)
                if current is not None and current.status == "pending" and current.is_expired():
                    # 决策 CAS 输给了过期竞态：先原子完成过期转换，再如实返回 conflict。
                    self.expire_one(approval_request_id)
                    raise conflict("This approval request has expired.")
                if current is None or current.status != "rejected":
                    raise conflict("The approval decision could not be applied.")
            elif terminal is not None:
                self.events.notify(terminal)
            return DecisionResult(
                approval_request_id,
                "rejected",
                original.run_id,
                "resume_scheduled",
                None,
                idempotent_replay=not applied,
            )

        # Approve：决策 CAS、successor Run 创建与绑定、原始 Run 的 handoff 终态在**同一个
        # 事务**内提交。进程在任一步骤退出都会整体回滚，绝不会留下「approved 但没有
        # successor」的不可恢复状态；重复/并发 approve 共享同一已提交结果。
        successor_run_id = new_id()
        successor = None
        terminal: EventEnvelope | None = None
        with self.pool.transaction() as cursor:
            applied = decide_with_cursor(
                cursor,
                approval_request_id,
                lifecycle="approved",
                decided_by=user_id,
                successor_run_id=successor_run_id,
            )
            if applied:
                successor = insert_run_row(
                    cursor,
                    run_id=successor_run_id,
                    conversation_id=original.conversation_id,
                    thread_id=original.thread_id,
                    user_id=original.user_id,
                    user_input=original.user_input,
                    status="accepted",
                    predecessor_run_id=original.run_id,
                    original_run_id=original.run_id,
                    approval_request_id=approval_request_id,
                )
                # 原始 Run 的终态与它的关闭事件同样在这个事务内落库：
                # 提交后进程退出也不会出现「已是 handoff_complete 但流没有 final」。
                terminal = apply_terminal_transition(
                    cursor,
                    runs=self.runs,
                    events=self.events,
                    transition=TerminalTransition(
                        run_id=original.run_id,
                        thread_id=original.thread_id,
                        status="handoff_complete",
                        event_type="final",
                        payload={
                            "status": "handoff_complete",
                            "content": (
                                "The export was approved; execution continues in the successor run."
                            ),
                            "successor_run_id": successor.run_id,
                        },
                        logical_transition="handoff_complete",
                        required_status="awaiting_approval",
                    ),
                )
                if terminal is None:
                    # successor 与 approval 决策必须和原 Run 的 handoff 终态同事务提交。
                    # CAS 失败时抛出异常，确保事务回滚而不是留下半完成 successor。
                    raise conflict("The approval decision could not be applied to the waiting run.")
        if not applied or successor is None:
            # 本次调用没有完成转换：返回同一逻辑结果（含已登记的 successor），不再有任何
            # 副作用；API 层据此不会再次 dispatch。
            current = self.approvals.get(approval_request_id)
            if current is not None and current.status == "pending" and current.is_expired():
                # 决策 CAS 输给了过期竞态：先原子完成过期转换，再如实返回 conflict，
                # 绝不因为「差点被批准」而让审批停在 pending。
                self.expire_one(approval_request_id)
                raise conflict("This approval request has expired.")
            if current is None or current.status != "approved":
                raise conflict("This approval request already reached a terminal decision.")
            return DecisionResult(
                approval_request_id,
                "approved",
                original.run_id,
                "resume_scheduled",
                current.successor_run_id,
                idempotent_replay=True,
            )
        if terminal is not None:
            # 事务提交后扇出：等待中的客户端能观察到 handoff 终态并关闭流。
            self.events.notify(terminal)
        return DecisionResult(
            approval_request_id,
            "approved",
            original.run_id,
            "resume_scheduled",
            successor.run_id,
            idempotent_replay=False,
        )

    def expire_due(self, *, limit: int = 100) -> list[str]:
        expired: list[str] = []
        for approval in self.approvals.list_due_pending(limit=limit):
            if self.expire_one(approval.approval_request_id):
                expired.append(approval.approval_request_id)
        return expired

    def finalize_expired(self, approval_request_id: str) -> bool:
        """补齐「审批已是 ``expired``、但 Run 仍 ``awaiting_approval``」的历史残留。

        这类残留只可能由非原子的过期写入留下（历史版本或异常中断）。转换本身是幂等的：
        审批已处于终态，这里只做 Run 的条件终结与终态事件，`event_id` 与正常过期路径
        完全一致，因此绝不会产生第二条 final。
        """

        approval = self.approvals.get(approval_request_id)
        if approval is None or approval.status != "expired":
            return False
        run = self.runs.get(approval.run_id)
        if run is None or run.status != "awaiting_approval":
            return False
        return record_terminal_transition(
            pool=self.pool,
            runs=self.runs,
            events=self.events,
            transition=TerminalTransition(
                run_id=run.run_id,
                thread_id=run.thread_id,
                status="expired",
                event_type="final",
                payload={
                    "status": "expired",
                    "content": "The approval request expired. No export was created.",
                },
                logical_transition="approval_expired",
                required_status="awaiting_approval",
                error_code="approval_expired",
                error_message="The approval request expired without a decision.",
            ),
        )

    def expire_one(self, approval_request_id: str) -> bool:
        """把单个待决策审批原子地过期：审批 CAS 与原 Run 终态、终态事件同事务提交。

        返回 True 表示本次调用完成了转换；False 表示没有 pending 审批可过期或
        Run 已不在等待状态（CAS 幂等，绝不覆盖终态）。
        """

        approval = self.approvals.get(approval_request_id)
        if approval is None or approval.status != "pending":
            return False
        run = self.runs.get(approval.run_id)
        if run is None or run.status != "awaiting_approval":
            # 原 Run 已离开等待态（已被 Reject 终结等）：仍把审批单独转为 expired，
            # 避免过期审批长期停留在可决策集合里。
            return self.approvals.mark_expired(approval_request_id)
        terminal: EventEnvelope | None = None
        with self.pool.transaction() as cursor:
            applied = expire_with_cursor(cursor, approval_request_id)
            if applied:
                # 过期同样是终态转换：状态与关闭事件在同一事务内提交
                # （提交后进程退出也不会留下「已 expired 但没有 final」的流）。
                terminal = apply_terminal_transition(
                    cursor,
                    runs=self.runs,
                    events=self.events,
                    transition=TerminalTransition(
                        run_id=run.run_id,
                        thread_id=run.thread_id,
                        status="expired",
                        event_type="final",
                        payload={
                            "status": "expired",
                            "content": "The approval request expired. No export was created.",
                        },
                        logical_transition="approval_expired",
                        required_status="awaiting_approval",
                        error_code="approval_expired",
                        error_message="The approval request expired without a decision.",
                    ),
                )
                if terminal is None:
                    # Expiry CAS and the waiting-Run CAS must commit or roll back together.
                    raise conflict("The approval expiry could not be applied to the waiting run.")
        if applied and terminal is not None:
            self.events.notify(terminal)
        return applied


__all__ = ["ApprovalService", "AppError", "DecisionResult"]
