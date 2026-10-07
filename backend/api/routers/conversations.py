"""Conversation and Message endpoints (API-003)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from backend.api.dependencies import Container, get_container, get_current_user
from backend.api.schemas import (
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationResponse,
    MessageListResponse,
    MessageResponse,
)
from backend.domain.auth import AuthenticatedUser
from backend.domain.errors import not_found, validation_error
from backend.domain.identifiers import to_rfc3339
from backend.infrastructure.repositories.conversation import ConversationRecord

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _conversation_payload(record: ConversationRecord) -> ConversationResponse:
    return ConversationResponse(
        conversation_id=record.conversation_id,
        title=record.title,
        status=record.status,
        created_at=to_rfc3339(record.created_at) or "",
        updated_at=to_rfc3339(record.updated_at) or "",
    )


@router.post("", response_model=ConversationResponse, status_code=201)
def create_conversation(
    payload: ConversationCreateRequest,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConversationResponse:
    record = container.runtime.conversations.create(
        user_id=user.user_id, title=(payload.title or "New conversation")
    )
    return _conversation_payload(record)


@router.get("", response_model=ConversationListResponse)
def list_conversations(
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None, max_length=64),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConversationListResponse:
    settings = container.settings
    resolved = settings.conversations_default_limit if limit is None else limit
    if resolved > settings.conversations_max_limit:
        raise validation_error("The requested limit exceeds the maximum.")
    items, next_cursor = container.runtime.conversations.list(
        user_id=user.user_id, limit=resolved, cursor=cursor
    )
    return ConversationListResponse(
        items=[_conversation_payload(item) for item in items], next_cursor=next_cursor
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(
    conversation_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConversationResponse:
    record = container.runtime.conversations.get(conversation_id, user_id=user.user_id)
    if record is None:
        raise not_found("Conversation was not found.")
    return _conversation_payload(record)


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
def list_messages(
    conversation_id: str,
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None, max_length=64),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MessageListResponse:
    if container.runtime.conversations.get(conversation_id, user_id=user.user_id) is None:
        raise not_found("Conversation was not found.")
    settings = container.settings
    resolved = settings.messages_default_limit if limit is None else limit
    if resolved > settings.messages_max_limit:
        raise validation_error("The requested limit exceeds the maximum.")
    items, next_cursor = container.runtime.conversations.list_messages(
        conversation_id=conversation_id, limit=resolved, cursor=cursor
    )
    return MessageListResponse(
        items=[
            MessageResponse(
                message_id=item.message_id,
                role=item.role,
                content=item.content,
                status=item.status,
                run_id=item.run_id,
                citations=item.citations,
                created_at=to_rfc3339(item.created_at) or "",
            )
            for item in items
        ],
        next_cursor=next_cursor,
    )


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    if not container.runtime.conversations.archive(conversation_id, user_id=user.user_id):
        raise not_found("Conversation was not found.")
    return Response(status_code=204)
