"""Document、Ingestion Job 与 Structured Data 管理端点（API-007 / API-008）。"""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile

from backend.api.dependencies import (
    Container,
    get_container,
    get_current_user,
)
from backend.api.schemas import (
    ColumnResponse,
    DataSourceListResponse,
    DataSourceResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadResponse,
    IngestionJobResponse,
    PreviewResponse,
    TableDetailResponse,
    TableListResponse,
    TableResponse,
)
from backend.application.ingestion import UploadOutcome
from backend.domain.auth import AuthenticatedUser
from backend.domain.errors import AppError, conflict, not_found, validation_error
from backend.domain.identifiers import to_rfc3339
from backend.infrastructure.file_storage import UploadStorageError
from backend.infrastructure.parsers import supported_content_type
from backend.infrastructure.repositories.documents import DocumentRecord

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data"])

# 上传分块大小：API 逐块读入受控存储，内存占用与文件大小无关。
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _document_payload(record: DocumentRecord) -> DocumentResponse:
    return DocumentResponse(
        document_id=record.document_id,
        title=record.title,
        source_name=record.source_name,
        status=record.status,
        active_index_version=record.active_index_version,
        byte_size=record.byte_size,
        created_at=to_rfc3339(record.created_at) or "",
        updated_at=to_rfc3339(record.updated_at) or "",
    )


def _upload_rejection_message(error: UploadStorageError) -> str:
    code = str(error)
    if code == "document_too_large":
        return "The uploaded file exceeds the configured size limit."
    if code == "storage_unavailable":
        return "The service cannot accept uploads right now; please retry later."
    return "The uploaded file could not be stored safely."


def _upload_rejection_status(error: UploadStorageError) -> int:
    """容量不足是服务端状态问题（409 conflict），不是请求内容问题（422）。"""

    return 409 if str(error) == "storage_unavailable" else 422


def _upload_error(error: UploadStorageError) -> AppError:
    message = _upload_rejection_message(error)
    if _upload_rejection_status(error) == 409:
        return conflict(message)
    return validation_error(message)


def _schedule_ingestion(container: Container, outcome: UploadOutcome) -> None:
    """立即调度摄取 Job；容量耗尽**不**报错。

    原文件与 Job 已经持久化（``document_files`` + ``pending`` Job），维护任务每周期用
    同一个有界 worker 补调度（``MaintenanceWorker.schedule_pending_jobs``），因此不会
    出现「返回错误但留下无法恢复的 pending」或「文档已创建但客户端拿不到 ID」。
    """

    if container.ingestion_worker.submit(ingestion_job_id=outcome.ingestion_job_id):
        return
    logger.warning("ingestion_schedule_deferred job=%s", outcome.ingestion_job_id[:8])


def _upload_payload(outcome: UploadOutcome, *, duplicate: bool) -> DocumentUploadResponse:
    return DocumentUploadResponse(
        document_id=outcome.document_id,
        ingestion_job_id=outcome.ingestion_job_id,
        index_version=outcome.index_version,
        status=outcome.status,
        duplicate=duplicate,
    )


@router.post("/documents", response_model=DocumentUploadResponse, status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentUploadResponse:
    filename = file.filename or "document.txt"
    content_type = file.content_type or "application/octet-stream"
    settings = container.settings
    # 格式门控先于落盘：不支持的类型不会在受控存储里留下任何字节。
    if not supported_content_type(
        content_type, filename, allow_remote_parser=settings.parser_provider == "mineru"
    ):
        raise validation_error("This document format is not supported.")

    storage = container.runtime.upload_storage
    try:
        # 余量门控先于接收字节：容量不足时明确拒绝（409），不会写到一半磁盘满（ADR-036）。
        storage.ensure_capacity(min_free_bytes=settings.min_storage_free_bytes)
    except UploadStorageError as error:
        raise _upload_error(error) from None
    writer = storage.begin()
    try:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
    except UploadStorageError as error:
        # 超限/写入失败：abort() 只删除暂存件，绝不发布半份文件。
        writer.abort()
        raise _upload_error(error) from None
    except Exception:
        writer.abort()
        raise
    stored = writer.finish()
    if stored.byte_size == 0:
        # 空文件永远不会被任何 Document 引用，直接清理。
        storage.delete(stored.storage_key)
        raise validation_error("The uploaded file is empty.")

    try:
        outcome = container.runtime.ingestion.submit_upload(
            user_id=user.user_id,
            filename=filename,
            content_type=content_type,
            stored=stored,
            idempotency_key=idempotency_key,
        )
    except AppError:
        # 登记被拒：只有确认当前用户没有引用该内容时才清理文件，
        # 避免删除重复上传正在使用的原文件（内容寻址会命中同一条目）。
        existing = container.runtime.documents.find_by_fingerprint(
            user_id=user.user_id, fingerprint=stored.sha256
        )
        if existing is None:
            storage.delete(stored.storage_key)
        raise

    if not outcome.duplicate:
        _schedule_ingestion(container, outcome)
    return _upload_payload(outcome, duplicate=outcome.duplicate)


@router.get("/documents", response_model=DocumentListResponse)
def list_documents(
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None, max_length=64),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentListResponse:
    settings = container.settings
    resolved = settings.documents_default_limit if limit is None else limit
    if resolved > settings.documents_max_limit:
        raise validation_error("The requested limit exceeds the maximum.")
    items, next_cursor = container.runtime.documents.list(
        user_id=user.user_id, limit=resolved, cursor=cursor
    )
    return DocumentListResponse(
        items=[_document_payload(item) for item in items], next_cursor=next_cursor
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentResponse:
    record = container.runtime.documents.get(document_id, user_id=user.user_id)
    if record is None:
        raise not_found("Document was not found.")
    return _document_payload(record)


@router.delete("/documents/{document_id}", response_model=DocumentUploadResponse, status_code=202)
def delete_document(
    document_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentUploadResponse:
    outcome = container.runtime.ingestion.submit_delete(
        user_id=user.user_id, document_id=document_id
    )
    _schedule_ingestion(container, outcome)
    return _upload_payload(outcome, duplicate=False)


@router.post(
    "/documents/{document_id}/reindex", response_model=DocumentUploadResponse, status_code=202
)
def reindex_document(
    document_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentUploadResponse:
    outcome = container.runtime.ingestion.submit_reindex(
        user_id=user.user_id, document_id=document_id
    )
    _schedule_ingestion(container, outcome)
    return _upload_payload(outcome, duplicate=False)


@router.get("/ingestion-jobs/{ingestion_job_id}", response_model=IngestionJobResponse)
def get_ingestion_job(
    ingestion_job_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> IngestionJobResponse:
    job = container.runtime.ingestion.get_job(
        user_id=user.user_id, ingestion_job_id=ingestion_job_id
    )
    return IngestionJobResponse(
        ingestion_job_id=job.ingestion_job_id,
        document_id=job.document_id,
        index_version=job.index_version,
        job_type=job.job_type,
        status=job.status,
        attempt=job.attempt,
        failure_code=job.failure_code,
        failure_message=job.failure_message,
        created_at=to_rfc3339(job.created_at) or "",
        updated_at=to_rfc3339(job.updated_at) or "",
        finished_at=to_rfc3339(job.finished_at),
    )


@router.post(
    "/ingestion-jobs/{ingestion_job_id}/retry",
    response_model=DocumentUploadResponse,
    status_code=202,
)
def retry_ingestion_job(
    ingestion_job_id: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentUploadResponse:
    outcome = container.runtime.ingestion.submit_retry(
        user_id=user.user_id, ingestion_job_id=ingestion_job_id
    )
    _schedule_ingestion(container, outcome)
    return _upload_payload(outcome, duplicate=False)


# Structured Data 检查 API ------------------------------------------------
def _schema_tables(container: Container) -> list[TableResponse]:
    from backend.tools.data_tools import DatabaseSchemaInput, DatabaseSchemaTool

    tool = DatabaseSchemaTool(
        settings=container.settings, query_runner=container.runtime.query_runner
    )
    result = tool.execute(
        "api-schema", DatabaseSchemaInput(table_names=list(container.settings.allowed_table_names))
    )
    if result.status != "success":
        return []
    return [
        TableResponse(
            table_name=str(table.get("table_name")),
            description=table.get("description"),
            column_count=len(table.get("columns") or []),
        )
        for table in (result.data or {}).get("tables", [])
    ]


@router.get("/data-sources", response_model=DataSourceListResponse)
def list_data_sources(
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DataSourceListResponse:
    del user
    tables = _schema_tables(container)
    return DataSourceListResponse(
        items=[
            DataSourceResponse(
                data_source_id=container.settings.data_source_id,
                schema_version=container.settings.schema_version,
                table_count=len(tables),
            )
        ]
    )


@router.get("/data-sources/{data_source_id}/tables", response_model=TableListResponse)
def list_tables(
    data_source_id: str,
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None, max_length=64),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TableListResponse:
    del user
    settings = container.settings
    if data_source_id != settings.data_source_id:
        raise not_found("Data source was not found.")
    resolved = settings.tables_default_limit if limit is None else limit
    if resolved > settings.tables_max_limit:
        raise validation_error("The requested limit exceeds the maximum.")
    tables = _schema_tables(container)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    window = tables[offset : offset + resolved]
    next_cursor = str(offset + resolved) if offset + resolved < len(tables) else None
    return TableListResponse(
        items=window, schema_version=settings.schema_version, next_cursor=next_cursor
    )


@router.get(
    "/data-sources/{data_source_id}/tables/{table_name}", response_model=TableDetailResponse
)
def get_table(
    data_source_id: str,
    table_name: str,
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TableDetailResponse:
    from backend.tools.data_tools import DatabaseSchemaInput, DatabaseSchemaTool

    del user
    settings = container.settings
    if data_source_id != settings.data_source_id:
        raise not_found("Data source was not found.")
    if table_name not in settings.allowed_table_names:
        raise not_found("Table was not found.")
    tool = DatabaseSchemaTool(settings=settings, query_runner=container.runtime.query_runner)
    result = tool.execute("api-table", DatabaseSchemaInput(table_names=[table_name]))
    if result.status != "success" or not (result.data or {}).get("tables"):
        raise not_found("Table was not found.")
    table = (result.data or {})["tables"][0]
    return TableDetailResponse(
        table_name=str(table.get("table_name")),
        description=table.get("description"),
        schema_version=settings.schema_version,
        columns=[
            ColumnResponse(
                name=str(column.get("name")),
                type=str(column.get("type")),
                nullable=bool(column.get("nullable")),
                masked=bool(column.get("masked")),
            )
            for column in table.get("columns") or []
        ],
    )


@router.get(
    "/data-sources/{data_source_id}/tables/{table_name}/preview",
    response_model=PreviewResponse,
)
def preview_table(
    data_source_id: str,
    table_name: str,
    limit: int | None = Query(default=None, ge=1),
    cursor: str | None = Query(default=None, max_length=64),
    container: Container = Depends(get_container),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PreviewResponse:
    """受控、已授权、只读的预览接口。不接受任何 SQL。"""

    del user
    settings = container.settings
    if data_source_id != settings.data_source_id:
        raise not_found("Data source was not found.")
    if table_name not in settings.allowed_table_names:
        raise not_found("Table was not found.")
    resolved = settings.preview_default_rows if limit is None else limit
    if resolved > settings.preview_max_rows:
        raise validation_error("The requested limit exceeds the maximum.")
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    columns = _table_columns(container, table_name)
    if not columns:
        raise not_found("Table was not found.")
    # table_name 已对照 allowlist，列名来自 schema，取值用绑定参数，不拼接用户输入。
    select_list = ", ".join(f"`{column}`" for column in columns)
    outcome = container.runtime.query_runner.run(
        f"SELECT {select_list} FROM `{table_name}` LIMIT %s OFFSET %s",
        (resolved + 1, offset),
        max_rows=resolved + 1,
    )
    if outcome.status != "success":
        raise validation_error("The preview could not be produced safely.")
    rows = [
        [
            _preview_value(value, column, settings.masked_column_names)
            for column, value in zip(columns, row, strict=False)
        ]
        for row in outcome.rows[:resolved]
    ]
    truncated = outcome.truncated or len(outcome.rows) > resolved
    next_cursor = str(offset + resolved) if truncated else None
    return PreviewResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        next_cursor=next_cursor,
    )


def _table_columns(container: Container, table_name: str) -> list[str]:
    from backend.tools.data_tools import DatabaseSchemaInput, DatabaseSchemaTool

    tool = DatabaseSchemaTool(
        settings=container.settings, query_runner=container.runtime.query_runner
    )
    result = tool.execute("api-columns", DatabaseSchemaInput(table_names=[table_name]))
    if result.status != "success" or not (result.data or {}).get("tables"):
        return []
    table = (result.data or {})["tables"][0]
    return [str(column.get("name")) for column in table.get("columns") or []]


def _preview_value(value, column: str, masked_columns: tuple[str, ...]):
    # 脱敏列固定长度遮挡，其余大字段统一截断，避免预览泄露敏感或超长内容。
    if column.casefold() in masked_columns:
        if value is None or value == "":
            return ""
        return "*" * min(len(str(value)), 8)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")[:500]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
