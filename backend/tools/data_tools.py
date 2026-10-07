"""Contract Tool implementations: knowledge_search, database_schema, database_query, data_export.

Every Tool receives explicit input and returns a normalized Tool Result. Tools never
read or mutate Graph State, Conversation Persistence, or Checkpoint.
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.infrastructure.config import Settings
from backend.infrastructure.database import DatabaseUnavailableError, ReadOnlyQueryRunner
from backend.infrastructure.embedding import EmbeddingProvider, EmbeddingProviderError
from backend.infrastructure.repositories.approvals import ApprovalRepository
from backend.infrastructure.repositories.documents import (
    ChunkRecord,
    ChunkRepository,
    DocumentRepository,
    IndexVersionRepository,
)
from backend.infrastructure.repositories.structured import (
    ExportArtifactRepository,
    QueryResultRepository,
)
from backend.infrastructure.vector_store import VectorStore, VectorStoreError, collection_name
from backend.tools.contracts import (
    RegisteredTool,
    ToolError,
    ToolExecutionContext,
    ToolMetadata,
    ToolResult,
)
from backend.tools.sql_validator import ProjectSQLValidator, SQLValidator


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    index_version: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)


class DatabaseSchemaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_source_id: str | None = None
    table_names: list[str] = Field(default_factory=list, max_length=20)


class DatabaseQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_source_id: str | None = None
    sql: str = Field(min_length=1, max_length=5000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_rows: int = Field(default=100, ge=1, le=1000)


class DataExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="database_query_result", max_length=40)
    query_result_id: str = Field(min_length=1, max_length=64)
    format: str = Field(default="csv", max_length=10)
    columns: list[str] = Field(default_factory=list, max_length=200)
    filters: dict[str, str] = Field(default_factory=dict)
    destination: str = Field(default="managed_export_area", max_length=40)
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeSearchTool(RegisteredTool):
    """Dense retrieval over Child Chunks with Parent mapping handled by the Graph."""

    metadata = ToolMetadata("knowledge_search", "1", "low", "knowledge_retrieval")
    input_model = KnowledgeSearchInput

    # Bounded retrieval windows: `knowledge_search` is never an unbounded scan.
    RETRIEVAL_CANDIDATE_FACTOR = 4
    MAX_RETRIEVAL_CANDIDATES = 200
    MAX_DOCUMENT_SCOPE = 50

    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
        versions: IndexVersionRepository,
        documents: DocumentRepository,
        chunks: ChunkRepository,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.vectors = vectors
        self.versions = versions
        self.documents = documents
        self.chunks = chunks

    def execute(
        self,
        tool_call_id: str,
        arguments: KnowledgeSearchInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        top_k = min(arguments.top_k, self.settings.max_retrieval_top_k)
        documents = self._authorized_documents(arguments, context)
        if not documents:
            return self._success(tool_call_id, arguments, [], started, truncated=False)

        try:
            embedding = self.embeddings.embed(arguments.query)
        except EmbeddingProviderError as error:
            return self._failed(
                tool_call_id, str(error), "Knowledge retrieval is temporarily unavailable."
            )
        default_collection = self._collection_for(embedding)

        if arguments.index_version:
            version = self.versions.get(arguments.index_version)
            if version is None or version.status != "active":
                return self._rejected(
                    tool_call_id, "index_version_not_active", "That index version is not active."
                )
            targets = [(version.index_version, version.collection_name or default_collection)]
        else:
            # Each authorized Document is searched through its own active version, which
            # keeps the query bounded and avoids one wide `document_id IN (...)` filter.
            targets = []
            for document in documents[: self.MAX_DOCUMENT_SCOPE]:
                if not document.active_index_version:
                    continue
                version = self.versions.get(document.active_index_version)
                if version is None or version.status != "active":
                    continue
                targets.append(
                    (version.index_version, version.collection_name or default_collection)
                )
            if not targets:
                return self._success(tool_call_id, arguments, [], started, truncated=False)

        groups = []
        for index_version, collection in targets:
            try:
                groups.append(
                    self._search(
                        collection=collection,
                        embedding=embedding,
                        top_k=top_k,
                        index_version=index_version,
                    )
                )
            except VectorStoreError as error:
                return self._failed(
                    tool_call_id, str(error), "Knowledge retrieval is temporarily unavailable."
                )

        allowed_ids = {document.document_id for document in documents}
        merged: dict[str, float] = {}
        for group in groups:
            for match in group:
                # Authorization is enforced on the returned rows as well as by the
                # server-resolved scope, so no result can escape it.
                if not match.document_id or match.document_id not in allowed_ids:
                    continue
                if match.score > merged.get(match.chunk_id, float("-inf")):
                    merged[match.chunk_id] = match.score
        if not merged:
            return self._success(tool_call_id, arguments, [], started, truncated=False)
        ranked = sorted(merged.items(), key=lambda item: item[1], reverse=True)

        chunks = self.chunks.list_by_ids([chunk_id for chunk_id, _ in ranked], index_version=None)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        results: list[dict[str, Any]] = []
        for chunk_id, score in ranked:
            chunk = by_id.get(chunk_id)
            if chunk is None or chunk.document_id not in allowed_ids:
                continue
            results.append(self._result_item(chunk, score))
            if len(results) >= top_k:
                break
        truncated = len(results) >= top_k
        return self._success(tool_call_id, arguments, results, started, truncated=truncated)

    def _collection_for(self, embedding: list[float]) -> str:
        return collection_name(
            provider=self.embeddings.provider_name,
            model=self.embeddings.model,
            dimension=len(embedding),
        )

    def _search(self, *, collection: str, embedding: list[float], top_k: int, index_version: str):
        """Bounded vector search scoped by index version.

        Milvus is not asked for a large ``document_id IN (...)`` expression: that
        composite filter can under-recall on this runtime, so the query is scoped by
        index version and authorization is enforced on the returned rows instead.
        The candidate window stays bounded.
        """

        window = max(top_k, self.settings.max_retrieval_top_k) * self.RETRIEVAL_CANDIDATE_FACTOR
        return self.vectors.search(
            collection=collection,
            embedding=embedding,
            top_k=min(window, self.MAX_RETRIEVAL_CANDIDATES),
            index_version=index_version,
        )

    def _authorized_documents(
        self, arguments: KnowledgeSearchInput, context: ToolExecutionContext | None
    ):
        """Resolve candidate Documents inside the server-side authorization scope.

        The caller scope is authoritative. Requested `document_ids` may only narrow
        it; a request for a document outside the scope returns no evidence.
        """

        scope = set(context.authorized_document_ids) if context else set()
        requested = set(arguments.document_ids)
        candidates = scope & requested if requested else set(scope)
        allowed: list[Any] = []
        for document_id in sorted(candidates):
            document = self.documents.get(document_id)
            if document is not None and document.status == "available":
                allowed.append(document)
        return allowed

    def _result_item(self, chunk: ChunkRecord, score: float) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "parent_chunk_id": chunk.parent_chunk_id,
            "index_version": chunk.index_version,
            "content": chunk.content,
            "score": round(float(score), 6),
            "metadata": {"label": chunk.label, "location": chunk.location},
            "citation": {
                "source_id": chunk.document_id,
                "location": chunk.location,
                "label": chunk.label,
            },
        }

    def _success(
        self,
        tool_call_id: str,
        arguments: KnowledgeSearchInput,
        results: list[dict[str, Any]],
        started: float,
        *,
        truncated: bool,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={"query": arguments.query, "results": results, "truncated": truncated},
            metadata={
                "duration_ms": int((time.monotonic() - started) * 1000),
                "truncated": truncated,
            },
        )

    def _rejected(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="rejected",
            error=ToolError(code=code, message=message),
        )

    def _failed(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="failed",
            error=ToolError(code=code[:64], message=message),
        )


class DatabaseSchemaTool(RegisteredTool):
    """Allowlisted schema discovery from the read-only account; no system catalogs."""

    metadata = ToolMetadata("database_schema", "1", "low", "structured_schema")
    input_model = DatabaseSchemaInput

    def __init__(self, *, settings: Settings, query_runner: ReadOnlyQueryRunner) -> None:
        self.settings = settings
        self.query_runner = query_runner

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseSchemaInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        requested = [name.casefold() for name in arguments.table_names]
        allowed = [
            table
            for table in self.settings.allowed_table_names
            if not requested or table.casefold() in requested
        ]
        if requested and not allowed:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(
                    code="table_not_allowed", message="The requested table is not available."
                ),
            )
        if not allowed:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="success",
                data={
                    "data_source_id": arguments.data_source_id or self.settings.data_source_id,
                    "schema_version": self.settings.schema_version,
                    "tables": [],
                },
            )
        placeholders = ", ".join(["%s"] * len(allowed))
        outcome = self.query_runner.run(
            "SELECT table_name, column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name IN ({placeholders}) "
            "ORDER BY table_name, ordinal_position",
            (self.settings.mysql_database, *allowed),
            max_rows=500,
        )
        if outcome.status != "success":
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code=outcome.error_code or "database_error",
                    message="Schema metadata is temporarily unavailable.",
                ),
            )
        tables: dict[str, dict[str, Any]] = {
            name: {"table_name": name, "description": None, "columns": []} for name in allowed
        }
        for row in outcome.rows:
            table_name = str(row[0])
            table = tables.get(table_name)
            if table is None:
                continue
            column_name = str(row[1])
            table["columns"].append(
                {
                    "name": column_name,
                    "type": str(row[2]),
                    "nullable": str(row[3]).upper() == "YES",
                    "description": None,
                    "masked": column_name.casefold() in self.settings.masked_column_names,
                }
            )
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "data_source_id": arguments.data_source_id or self.settings.data_source_id,
                "schema_version": self.settings.schema_version,
                "tables": list(tables.values()),
            },
            metadata={"duration_ms": 0, "truncated": outcome.truncated},
        )


class DatabaseQueryTool(RegisteredTool):
    """`database_query`: Tool input validation -> SQL Validator -> read-only database."""

    metadata = ToolMetadata("database_query", "1", "medium", "structured_query")
    input_model = DatabaseQueryInput

    def __init__(
        self,
        *,
        settings: Settings,
        validator: SQLValidator,
        query_runner: ReadOnlyQueryRunner,
        results: QueryResultRepository,
    ) -> None:
        self.settings = settings
        self.validator = validator
        self.query_runner = query_runner
        self.results = results

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseQueryInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        validation = self._validate(arguments.sql)
        if not validation.accepted:
            # Security and policy rejections are terminal for this call and never retried.
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(
                    code="sql_rejected",
                    message="The query was rejected by the SQL Validator.",
                ),
                metadata={"sql_validation": "rejected", "reasons": list(validation.reasons)},
            )
        max_rows = min(arguments.max_rows, self.settings.sql_max_rows)
        try:
            # One extra row is read only so truncation is detected from the result set.
            outcome = self.query_runner.run(
                arguments.sql, arguments.parameters, max_rows=max_rows + 1
            )
        except DatabaseUnavailableError:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code="database_unavailable", message="The database is unavailable."
                ),
                metadata={"sql_validation": "accepted"},
            )
        if outcome.status != "success":
            code = outcome.error_code or "database_error"
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code=code,
                    message=(
                        "The query exceeded the configured time limit."
                        if code == "query_timeout"
                        else "The query could not be executed safely."
                    ),
                ),
                metadata={"sql_validation": "accepted"},
            )

        masked_columns = [
            index
            for index, column in enumerate(outcome.columns)
            if column.casefold() in self.settings.masked_column_names
        ]
        rows = [
            [
                _mask(value) if index in masked_columns else _safe_value(value)
                for index, value in enumerate(row)
            ]
            for row in outcome.rows
        ]
        duration_ms = int((time.monotonic() - started) * 1000)
        snapshot = self.results.create(
            user_id=(context.user_id if context and context.user_id else ""),
            conversation_id=context.conversation_id if context else None,
            run_id=context.run_id if context else None,
            data_source_id=arguments.data_source_id or self.settings.data_source_id,
            columns=list(outcome.columns),
            rows=[list(row) for row in rows],
            truncated=outcome.truncated,
            query_fingerprint=_fingerprint(arguments.sql),
            tables=sorted(_tables_of(arguments.sql)),
            duration_ms=duration_ms,
            retention_seconds=self.settings.query_result_retention_seconds,
        )
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "query_result_id": snapshot.query_result_id,
                "columns": list(outcome.columns),
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "truncated": outcome.truncated,
                "query_summary": {
                    "tables": sorted(_tables_of(arguments.sql)),
                    "duration_ms": duration_ms,
                },
            },
            metadata={"sql_validation": "accepted", "duration_ms": duration_ms},
        )

    def _validate(self, sql: str):
        validator: Any = self.validator
        return validator.validate(sql)


class DataExportTool(RegisteredTool):
    """High-risk Tool. Proposal creates the Approval Request; execution needs an approved one."""

    metadata = ToolMetadata("data_export", "1", "high", "controlled_export")
    input_model = DataExportInput

    def __init__(
        self,
        *,
        settings: Settings,
        approvals: ApprovalRepository,
        results: QueryResultRepository,
        artifacts: ExportArtifactRepository,
    ) -> None:
        self.settings = settings
        self.approvals = approvals
        self.results = results
        self.artifacts = artifacts

    def execute(
        self,
        tool_call_id: str,
        arguments: DataExportInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        scope = context or ToolExecutionContext()
        approval_request_id = scope.approval_request_id
        user_id = scope.user_id
        if scope.query_result_id:
            # The approved proposal bound one immutable snapshot; a resume cannot
            # substitute a newer query result for it.
            if arguments.query_result_id != scope.query_result_id:
                return self._rejected(
                    tool_call_id,
                    "approval_binding_mismatch",
                    "The approval does not match this export.",
                )
        if arguments.source != "database_query_result":
            return self._rejected(
                tool_call_id, "unsupported_source", "That export source is unsupported."
            )
        if arguments.format != "csv" or arguments.destination != "managed_export_area":
            return self._rejected(
                tool_call_id,
                "unsupported_destination",
                "Only CSV export to the managed export area is supported.",
            )

        query_result = self.results.get(arguments.query_result_id)
        if query_result is None:
            return self._rejected(
                tool_call_id,
                "query_result_not_found",
                "The referenced query result is unavailable.",
            )
        # A snapshot is only usable by the scope that produced it. A snapshot written
        # without an owner (for example a direct Tool call) is not transferable.
        if query_result.user_id != (user_id or ""):
            return self._rejected(
                tool_call_id,
                "query_result_not_authorized",
                "The referenced query result is unavailable.",
            )
        columns = [
            column
            for column in query_result.columns
            if not arguments.columns or column in arguments.columns
        ]
        if not columns or any(column not in query_result.columns for column in columns):
            return self._rejected(
                tool_call_id, "invalid_columns", "The requested columns are invalid."
            )
        masked = [
            column for column in columns if column.casefold() in self.settings.masked_column_names
        ]
        if masked:
            return self._rejected(
                tool_call_id,
                "sensitive_column_blocked",
                "The requested export includes a restricted column.",
            )
        if query_result.row_count > self.settings.export_max_rows:
            return self._rejected(
                tool_call_id, "export_too_large", "The query result exceeds the export size limit."
            )

        if approval_request_id is None:
            approval = self.approvals.create(
                run_id=scope.run_id or "",
                thread_id=scope.thread_id or "",
                conversation_id=scope.conversation_id or "",
                user_id=scope.user_id or "",
                summary={
                    "row_count": query_result.row_count,
                    "columns": columns,
                    "format": arguments.format,
                    "reason": arguments.reason[:200],
                },
                binding={
                    "query_result_id": query_result.query_result_id,
                    "columns": columns,
                    "format": arguments.format,
                    "destination": arguments.destination,
                    "reason": arguments.reason[:200],
                },
                expires_at=_expiry(self.settings.approval_expiry_seconds),
            )
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                data={
                    "approval_lifecycle": "pending",
                    "approval_request_id": approval.approval_request_id,
                    "summary": approval.summary,
                    "expires_at": approval.expires_at.isoformat(),
                },
                error=ToolError(
                    code="approval_required", message="Approval is required before export."
                ),
                metadata={"duration_ms": 0},
            )

        approval = self.approvals.get(approval_request_id)
        if approval is None:
            return self._rejected(
                tool_call_id, "approval_not_found", "The approval request is unavailable."
            )
        if approval.status == "rejected":
            return self._rejected(tool_call_id, "approval_rejected", "The export was not approved.")
        if approval.status == "expired" or approval.is_expired():
            self.approvals.mark_expired(approval.approval_request_id)
            return self._rejected(
                tool_call_id, "approval_expired", "The approval request has expired."
            )
        if approval.status != "approved":
            return self._rejected(
                tool_call_id, "approval_pending", "Approval is still required before export."
            )
        if approval.binding.get("query_result_id") != arguments.query_result_id:
            return self._rejected(
                tool_call_id,
                "approval_binding_mismatch",
                "The approval does not match this export.",
            )

        existing = self.artifacts.get_by_approval(approval.approval_request_id)
        if existing is not None:
            return self._completed(tool_call_id, approval, existing.artifact_id, existing.row_count)

        content = _to_csv(columns, query_result.rows, query_result.columns)
        created = self.artifacts.insert_ignore(
            artifact_id=_new_id(),
            approval_request_id=approval.approval_request_id,
            query_result_id=query_result.query_result_id,
            user_id=approval.user_id,
            format="csv",
            row_count=len(query_result.rows),
            content=content,
        )
        artifact = self.artifacts.get_by_approval(approval.approval_request_id)
        if artifact is None:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="export_failed", message="The export could not be created."),
            )
        self.approvals.record_artifact(
            approval.approval_request_id, artifact_id=artifact.artifact_id
        )
        return self._completed(
            tool_call_id, approval, artifact.artifact_id, artifact.row_count, created=created
        )

    def _completed(
        self,
        tool_call_id: str,
        approval: Any,
        artifact_id: str,
        row_count: int,
        *,
        created: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "status": "success",
                "approval_lifecycle": "approved",
                "approval_request_id": approval.approval_request_id,
                "artifact_id": artifact_id,
                "row_count": row_count,
                "format": "csv",
            },
            metadata={"duration_ms": 0, "created_now": created},
        )

    def _rejected(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="rejected",
            error=ToolError(code=code, message=message),
        )


def _safe_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")[:2000]
    if isinstance(value, str):
        return value[:2000]
    return value


def _mask(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    return "*" * min(len(text), 8)


def _fingerprint(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _tables_of(sql: str) -> set[str]:
    import sqlglot
    from sqlglot import exp

    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001 - validator already accepted this statement
        return set()
    return {table.name.casefold() for table in statement.find_all(exp.Table) if table.name}


def _to_csv(columns: list[str], rows: list[list[Any]], all_columns: list[str]) -> str:
    indices = [all_columns.index(column) for column in columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row[index] if index < len(row) else None for index in indices])
    return buffer.getvalue()


def _new_id() -> str:
    from backend.domain.identifiers import new_id

    return new_id()


def _expiry(seconds: int):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    return utc_now() + timedelta(seconds=seconds)


def build_tool_validator(settings: Settings) -> ProjectSQLValidator:
    from backend.tools.sql_validator import build_validator

    return build_validator(settings)
