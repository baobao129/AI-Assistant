from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ToolStatus = Literal["success", "rejected", "failed"]
ToolRisk = Literal["low", "medium", "high"]


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str = Field(max_length=500)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_id: str
    tool_call_id: str
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolMetadata:
    tool_id: str
    version: str
    risk: ToolRisk
    capability: str


@dataclass(frozen=True)
class ToolExecutionContext:
    """Explicit request scope passed to a Tool; it is not Graph State."""

    conversation_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    user_id: str | None = None
    approval_request_id: str | None = None
    # Immutable Query Result Snapshot bound by an approved proposal, when resuming.
    query_result_id: str | None = None
    # Server-resolved authorization scope; the model cannot widen it.
    authorized_document_ids: tuple[str, ...] = ()


class RegisteredTool(ABC):
    metadata: ToolMetadata
    input_model: type[BaseModel]

    @abstractmethod
    def execute(
        self,
        tool_call_id: str,
        arguments: BaseModel,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        raise NotImplementedError
