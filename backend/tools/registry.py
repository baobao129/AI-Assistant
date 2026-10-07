from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.tools.contracts import RegisteredTool, ToolError, ToolExecutionContext, ToolResult


class ToolRegistry:
    def __init__(self, tools: Iterable[RegisteredTool] = ()) -> None:
        self._tools = {tool.metadata.tool_id: tool for tool in tools}

    def register(self, tool: RegisteredTool) -> None:
        if tool.metadata.tool_id in self._tools:
            raise ValueError(f"Tool already registered: {tool.metadata.tool_id}")
        self._tools[tool.metadata.tool_id] = tool

    def get(self, tool_id: str) -> RegisteredTool:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise KeyError(f"Unknown Tool ID: {tool_id}") from exc

    def list_tool_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def disabled_tool_ids(self) -> tuple[str, ...]:
        return tuple(
            tool_id
            for tool_id, tool in sorted(self._tools.items())
            if bool(getattr(tool, "disabled", False))
        )


class ToolExecutor:
    """唯一的调用边界；它返回事实，且从不接收 Graph State。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def invoke(
        self,
        tool_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            tool = self.registry.get(tool_id)
        except KeyError:
            return ToolResult(
                tool_id=tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(code="unknown_tool", message="Tool is not available."),
            )
        if bool(getattr(tool, "disabled", False)):
            return ToolResult(
                tool_id=tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(code="tool_disabled", message="Tool is not available."),
            )
        try:
            parsed: BaseModel = tool.input_model.model_validate(arguments)
            return tool.execute(tool_call_id, parsed, context)
        except ValidationError:
            return ToolResult(
                tool_id=tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(code="validation_error", message="Tool input is invalid."),
            )
