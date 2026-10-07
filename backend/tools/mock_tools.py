from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.tools.contracts import (
    RegisteredTool,
    ToolError,
    ToolExecutionContext,
    ToolMetadata,
    ToolResult,
)
from backend.tools.registry import ToolRegistry
from backend.tools.sql_validator import ProjectSQLValidator, SQLValidator


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
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
    max_rows: int = Field(default=100, ge=1, le=100)


class DataExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    query_result_id: str
    format: str = "csv"
    columns: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)
    destination: str = "managed_export_area"
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeSearchTool(RegisteredTool):
    metadata = ToolMetadata("knowledge_search", "1", "low", "knowledge_retrieval")
    input_model = KnowledgeSearchInput

    def execute(
        self,
        tool_call_id: str,
        arguments: KnowledgeSearchInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # No enterprise evidence is fabricated by the Phase 1 development stub.
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={"query": arguments.query, "results": [], "truncated": False},
            metadata={"duration_ms": 0, "truncated": False},
        )


class DatabaseSchemaTool(RegisteredTool):
    metadata = ToolMetadata("database_schema", "1", "low", "structured_schema")
    input_model = DatabaseSchemaInput

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseSchemaInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "data_source_id": arguments.data_source_id or "demo",
                "schema_version": "stub-1",
                "tables": [],
            },
            metadata={"duration_ms": 0, "truncated": False},
        )


class DatabaseQueryTool(RegisteredTool):
    metadata = ToolMetadata("database_query", "1", "medium", "structured_query")
    input_model = DatabaseQueryInput

    def __init__(self, validator: SQLValidator | None = None) -> None:
        self.validator = validator or ProjectSQLValidator()

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseQueryInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # The validator boundary is explicit; the Phase 1 executor has no database side effect.
        validation = self.validator.validate(arguments.sql)
        if validation.status != "accepted":
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(code="sql_rejected", message="SQL did not pass the validator."),
                metadata={"sql_validation": validation.status, "reason": validation.reason},
            )
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "query_result_id": "stub-query-result",
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
            },
            metadata={"sql_validation": "accepted", "duration_ms": 0, "truncated": False},
        )


class DataExportTool(RegisteredTool):
    metadata = ToolMetadata("data_export", "1", "high", "controlled_export")
    input_model = DataExportInput

    def execute(
        self,
        tool_call_id: str,
        arguments: DataExportInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        # Approval is a Graph/Runtime boundary; no artifact is created in this skeleton.
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="rejected",
            data={"approval_lifecycle": "pending", "approval_request_id": "stub-approval"},
            error=ToolError(
                code="approval_required", message="Approval is required before export."
            ),
            metadata={"duration_ms": 0, "truncated": False},
        )


def default_registry() -> ToolRegistry:
    return ToolRegistry(
        [KnowledgeSearchTool(), DatabaseSchemaTool(), DatabaseQueryTool(), DataExportTool()]
    )
