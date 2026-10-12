"""应用 API schemas。请求、domain 与 infrastructure 的结构保持分离。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.state import MAX_DOCUMENT_SCOPE_IDS


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


# Requested Document IDs 只接受**不透明标识符**（ADR-039 / CC-API-002）：路径、URL、
# 文件名与查询串都会因为分隔符或非法字符被拒，作用域不能承载任何任意查询条件。
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_input: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(default=None, max_length=64)
    # 用户提交的文档作用域（**未经授权证明**）。缺省与空数组都表示「无作用域」。
    # 这里只做格式校验；授权校验属于 Application 层。
    document_ids: list[str] | None = None
    client_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("document_ids")
    @classmethod
    def _validate_document_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unique: list[str] = []
        for item in value:
            token = item.strip() if isinstance(item, str) else ""
            if not _DOCUMENT_ID_PATTERN.fullmatch(token):
                raise ValueError(
                    "Document identifiers must be opaque identifiers of at most 64 characters."
                )
            if token not in unique:
                unique.append(token)
        # 先去重（保留首次出现顺序）再判上限：重复项不占用名额。
        if len(unique) > MAX_DOCUMENT_SCOPE_IDS:
            raise ValueError(f"At most {MAX_DOCUMENT_SCOPE_IDS} document identifiers are allowed.")
        return unique


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
