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


class RegisteredTool(ABC):
    metadata: ToolMetadata
    input_model: type[BaseModel]

    @abstractmethod
    def execute(self, tool_call_id: str, arguments: BaseModel) -> ToolResult:
        raise NotImplementedError
