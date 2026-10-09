"""Chat、Agent Run 与 SSE 流式端点（API-004 / API-005）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from backend.api.dependencies import (
    Container,
    RunDispatcher,
    get_container,
    get_current_user,
    get_dispatcher,
)
from backend.api.schemas import RunAcceptedResponse, RunCreateRequest, RunResponse
from backend.application.events import RunStreamNotFound
from backend.domain.auth import AuthenticatedUser
from backend.domain.errors import conflict
from backend.domain.identifiers import to_rfc3339
from backend.infrastructure.repositories.conversation import RunRecord

router = APIRouter(tags=["runs"])


def _run_payload(record: RunRecord) -> RunResponse:
    return RunResponse(
        run_id=record.run_id,
        conversation_id=record.conversation_id,
        thread_id=record.thread_id,
        status=record.status,
        intent=record.intent,
        routing_outcome=record.routing_outcome,
        created_at=to_rfc3339(record.created_at) or "",
        updated_at=to_rfc3339(record.updated_at) or "",
        completed_at=to_rfc3339(record.completed_at),
    )


@router.post(
    "/conversations/{conversation_id}/runs",
    response_model=RunAcceptedResponse,
    status_code=202,
)
def create_run(
    conversation_id: str,
    payload: RunCreateRequest,
    container: Container = Depends(get_container),
    dispatcher: RunDispatcher = Depends(get_dispatcher),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RunAcceptedResponse:
    handle = container.runs.start_run(
        user_id=user.user_id,
        conversation_id=conversation_id,
        user_input=payload.user_input,
        thread_id=payload.thread_id,
        client_metadata=payload.client_metadata,
    )
    if not dispatcher.dispatch(handle.run_id):
        # 调度容量耗尽：这个 Run 永远不会开始执行，必须以安全终态收口（带终态事件），
        # 而不是停留在 ``accepted``；普通 Run 不在维护任务的恢复范围内，没有其他补救路径。
        container.runs.fail_run_safely(
            handle.run_id,
            code="scheduler_unavailable",
            message="The run could not be scheduled; please retry.",
            required_status="accepted",
        )
        raise conflict("The run could not be scheduled; please retry.")
    return RunAcceptedResponse(
        conversation_id=handle.conversation_id,
        thread_id=handle.thread_id,
        run_id=handle.run_id,
        status="accepted",
        stream_url=f"/api/v1/runs/{handle.run_id}/events",
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RunResponse:
    return _run_payload(container.runs.resolve_run(user_id=user.user_id, run_id=run_id))


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    run = container.runs.resolve_run(user_id=user.user_id, run_id=run_id)
    events = container.events
    # Last-Event-ID 超出保留的事件窗口时无法补发历史，按 409 语义要求客户端重新连接。
    try:
        history = events.history(run_id=run_id, after_event_id=last_event_id)
    except RunStreamNotFound:
        raise conflict(
            "The requested replay position is outside the retained event window."
        ) from None

    async def event_stream() -> AsyncIterator[bytes]:
        # 先补发历史事件再订阅实时事件；终态 Run 无需订阅，避免挂起空闲连接。
        last_replayed_sequence = 0
        for envelope in history:
            last_replayed_sequence = envelope.sequence
            yield envelope.to_sse().encode("utf-8")
        if run.status in {"completed", "rejected", "failed", "expired", "handoff_complete"}:
            return
        subscription = events.subscribe(run_id=run_id)
        loop = asyncio.get_running_loop()
        try:
            while True:
                if await request.is_disconnected():
                    return
                envelope = await loop.run_in_executor(
                    None, subscription.next, container.settings.sse_heartbeat_seconds
                )
                if envelope is None:
                    if subscription.closed:
                        return
                    if _is_waiting_for_approval(container, run_id):
                        # 等待人工决策属于暂停而非业务完成；此时不把客户端挂在空闲流上。
                        return
                    yield b": keep-alive\n\n"
                    continue
                if envelope.sequence <= last_replayed_sequence:
                    # 订阅通道可能仍持有补发窗口内的事件（读历史与订阅之间的竞态）；
                    # 这些事件已按 sequence 顺序补发过，必须丢弃，保证该 Run 的流严格递增
                    # 且不重复投递同一个 sequence。
                    continue
                last_replayed_sequence = envelope.sequence
                yield envelope.to_sse().encode("utf-8")
                if envelope.event_type == "approval_required":
                    # 暂停点已送达；决策后客户端改为监听后继 Run 的流。
                    return
        finally:
            subscription.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _is_waiting_for_approval(container: Container, run_id: str) -> bool:
    """Run 处于等待人工决策（而非仍在执行）时返回 True。"""

    run = container.runtime.runs.get(run_id)
    return bool(run and run.status == "awaiting_approval")
