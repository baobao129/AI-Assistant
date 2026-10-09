"""审批端点（API-006）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from backend.api.dependencies import (
    Container,
    RunDispatcher,
    get_container,
    get_current_user,
    get_dispatcher,
)
from backend.api.schemas import ApprovalDecisionRequest, ApprovalDecisionResponse
from backend.domain.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _decide(
    approval_request_id: str,
    payload: ApprovalDecisionRequest,
    container: Container,
    dispatcher: RunDispatcher,
    user: AuthenticatedUser,
    decision: str,
) -> ApprovalDecisionResponse:
    result = container.approvals.decide(
        approval_request_id,
        decision=decision,  # type: ignore[arg-type]
        user_id=user.user_id,
        conversation_id=payload.conversation_id,
        thread_id=payload.thread_id,
        run_id=payload.run_id,
    )
    stream_url = None
    if result.successor_run_id:
        if not result.idempotent_replay:
            # 只有真正完成本次批准转换的调用才调度 successor；重复 approve 绝不再次
            # dispatch，保证同一 approval_request_id 的获批执行最多发生一次（API-006）。
            # RunService.resume 还会以原子领取兜底，重复调度也不会产生第二次执行。
            if not dispatcher.dispatch(
                result.successor_run_id, approval_request_id=approval_request_id
            ):
                logger.warning("approval_successor_dispatch_capacity_exhausted")
        # 后继 Run 继续执行并拥有独立的 SSE 序列。
        stream_url = f"/api/v1/runs/{result.successor_run_id}/events"
    return ApprovalDecisionResponse(
        approval_request_id=result.approval_request_id,
        approval_lifecycle=result.approval_lifecycle,
        run_id=result.run_id,
        status=result.status,
        successor_run_id=result.successor_run_id,
        stream_url=stream_url,
    )


@router.post(
    "/{approval_request_id}/approve", response_model=ApprovalDecisionResponse, status_code=202
)
def approve(
    approval_request_id: str,
    payload: ApprovalDecisionRequest,
    container: Container = Depends(get_container),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalDecisionResponse:
    return _decide(approval_request_id, payload, container, dispatcher, user, "approve")


@router.post(
    "/{approval_request_id}/reject", response_model=ApprovalDecisionResponse, status_code=202
)
def reject(
    approval_request_id: str,
    payload: ApprovalDecisionRequest,
    container: Container = Depends(get_container),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalDecisionResponse:
    return _decide(approval_request_id, payload, container, dispatcher, user, "reject")
