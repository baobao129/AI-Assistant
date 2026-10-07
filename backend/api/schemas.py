"""Application API schemas. Request, domain, and infrastructure shapes stay separate."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    services: dict[str, str]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    user_id: str
    username: str
    display_name: str


class SessionResponse(BaseModel):
    token: str
    expires_at: str


class LoginResponse(BaseModel):
    user: UserResponse
    session: SessionResponse


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)


class ConversationResponse(BaseModel):
    conversation_id: str
    title: str
    status: str
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    next_cursor: str | None = None


class MessageResponse(BaseModel):
    message_id: str
    role: str
    content: str
    status: str
    run_id: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None = None


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_input: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(default=None, max_length=64)
    client_metadata: dict[str, Any] = Field(default_factory=dict)


class RunAcceptedResponse(BaseModel):
    conversation_id: str
    thread_id: str
    run_id: str
    status: str
    stream_url: str


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    thread_id: str
    status: str
    intent: str | None = None
    routing_outcome: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)


class ApprovalDecisionResponse(BaseModel):
    approval_request_id: str
    approval_lifecycle: str
    run_id: str
    status: str
    successor_run_id: str | None = None
    stream_url: str | None = None


class DocumentResponse(BaseModel):
    document_id: str
    title: str
    source_name: str
    status: str
    active_index_version: str | None = None
    byte_size: int
    created_at: str
    updated_at: str


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    next_cursor: str | None = None


class DocumentUploadResponse(BaseModel):
    document_id: str
    ingestion_job_id: str
    index_version: str
    status: str
    duplicate: bool


class IngestionJobResponse(BaseModel):
    ingestion_job_id: str
    document_id: str
    index_version: str
    job_type: str
    status: str
    attempt: int
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None


class DataSourceResponse(BaseModel):
    data_source_id: str
    schema_version: str
    table_count: int


class DataSourceListResponse(BaseModel):
    items: list[DataSourceResponse]


class TableResponse(BaseModel):
    table_name: str
    description: str | None = None
    column_count: int


class TableListResponse(BaseModel):
    items: list[TableResponse]
    schema_version: str
    next_cursor: str | None = None


class ColumnResponse(BaseModel):
    name: str
    type: str
    nullable: bool
    masked: bool


class TableDetailResponse(BaseModel):
    table_name: str
    description: str | None = None
    schema_version: str
    columns: list[ColumnResponse]


class PreviewResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    next_cursor: str | None = None
