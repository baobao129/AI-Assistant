"""Chat, Agent Run, and SSE streaming endpoints (API-004 / API-005)."""

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
    try:
        history = events.history(run_id=run_id, after_event_id=last_event_id)
    except RunStreamNotFound:
        raise conflict(
            "The requested replay position is outside the retained event window."
        ) from None

    async def event_stream() -> AsyncIterator[bytes]:
        for envelope in history:
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
                        # Waiting for a human decision. That is a pause, not business
                        # completion, and the client is not held on an idle stream.
                        return
                    yield b": keep-alive\n\n"
                    continue
                yield envelope.to_sse().encode("utf-8")
                if envelope.event_type == "approval_required":
                    # The pause point is delivered; the client listens to the successor
                    # stream after the decision.
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
    """True when the Run is paused for a human decision rather than still executing."""

    run = container.runtime.runs.get(run_id)
    return bool(run and run.status == "awaiting_approval")
