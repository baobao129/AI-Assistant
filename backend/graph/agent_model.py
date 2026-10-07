"""Structured Data SQL Agent model boundary (Graph-005).

``model_node`` proposes Tool Calls or a final answer. Capability, budgets, the Tool
Registry, and the Answer Validation step remain outside this module. Provider SDK
types never appear here; only project-owned records cross the boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from backend.domain.state import GraphState
from backend.infrastructure.config import Settings
from backend.infrastructure.llm import (
    CapabilityResolver,
    MockGeneralLLM,
    UnsupportedCapabilityError,
)

logger = logging.getLogger(__name__)

MAX_TOOLS_IN_PROMPT = 8


@dataclass(frozen=True)
class ToolCallProposal:
    tool_id: str
    tool_call_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentDecision:
    tool_calls: tuple[ToolCallProposal, ...] = ()
    answer: str | None = None
    error: dict[str, object] | None = None


class SQLAgentModel(Protocol):
    def decide(
        self,
        *,
        state: GraphState,
        tool_ids: tuple[str, ...],
        tool_results: list[dict[str, Any]],
        remaining_iterations: int,
    ) -> AgentDecision: ...


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    description: str
    schema: dict[str, Any]


def describe_tools(settings: Settings, tool_ids: tuple[str, ...]) -> tuple[ToolDescriptor, ...]:
    """Bounded Tool descriptions used for structured prompting; never raw SDK schemas."""

    tables = ", ".join(settings.allowed_table_names)
    catalog: dict[str, ToolDescriptor] = {
        "database_schema": ToolDescriptor(
            tool_id="database_schema",
            description=f"List allowlisted table columns. Allowed tables: {tables}.",
            schema={
                "type": "object",
                "properties": {
                    "data_source_id": {"type": ["string", "null"]},
                    "table_names": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        ),
        "database_query": ToolDescriptor(
            tool_id="database_query",
            description=(
                "Run one read-only SELECT against the allowlisted tables. "
                "A single statement, no SELECT *, no OFFSET, and a literal LIMIT "
                f"no greater than {settings.sql_max_rows} are required. "
                "Use named bound parameters with %(name)s placeholders instead of literals."
            ),
            schema={
                "type": "object",
                "properties": {
                    "data_source_id": {"type": ["string", "null"]},
                    "sql": {"type": "string"},
                    "parameters": {"type": "object"},
                    "max_rows": {"type": "integer", "minimum": 1, "maximum": settings.sql_max_rows},
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
        ),
        "data_export": ToolDescriptor(
            tool_id="data_export",
            description=(
                "Propose a controlled CSV export of an existing query result. Requires "
                "human approval; no export occurs during this call."
            ),
            schema={
                "type": "object",
                "properties": {
                    "query_result_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["csv"]},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "destination": {"type": "string", "enum": ["managed_export_area"]},
                    "reason": {"type": "string"},
                },
                "required": ["query_result_id", "reason"],
                "additionalProperties": False,
            },
        ),
    }
    return tuple(catalog[tool_id] for tool_id in tool_ids if tool_id in catalog)[
        :MAX_TOOLS_IN_PROMPT
    ]


def _bound_query_result_id(state: GraphState) -> str | None:
    """The immutable Query Result Snapshot bound by an approved export proposal.

    Resuming a HITL flow must reuse the approved snapshot, so the Structured Data
    model is told about it instead of querying again.
    """

    if state.get("interrupt_status") != "approved":
        return None
    return state.get("query_result_id") or None


def build_agent_messages(
    *,
    state: GraphState,
    descriptors: tuple[ToolDescriptor, ...],
    tool_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_lines = "\n".join(
        f"- {descriptor.tool_id}: {descriptor.description}\n  arguments schema: {descriptor.schema}"
        for descriptor in descriptors
    )
    system = (
        "You are the Structured Data SQL agent for an internal analytics application. "
        "Use only the provided tools. Every SQL statement must be a single read-only SELECT "
        "with an explicit literal LIMIT. Never invent tables or columns. "
        "When you have enough data, answer with a short factual summary that states the "
        "row count and whether the result was truncated. "
        "If the data is insufficient, say so instead of guessing.\n"
        f"Available tools:\n{tool_lines}"
    )
    bound = _bound_query_result_id(state)
    if bound:
        system += (
            "\nAn approved export proposal already binds the query result "
            f"{bound}. Call data_export with exactly that query_result_id; do not run "
            "another database_query."
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": state["user_input"]},
    ]
    for result in tool_results:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Tool result for "
                    f"{result.get('tool_id')}: status={result.get('status')}; "
                    f"data={_bounded(result.get('data'))}; error={_bounded(result.get('error'))}"
                ),
            }
        )
    return messages


def _bounded(value: Any, limit: int = 4000) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


class LLMSQLAgentModel:
    """OpenAI-compatible tool-calling adapter owned by the project, not by a Provider SDK."""

    def __init__(self, *, provider: Any, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings

    def decide(
        self,
        *,
        state: GraphState,
        tool_ids: tuple[str, ...],
        tool_results: list[dict[str, Any]],
        remaining_iterations: int,
    ) -> AgentDecision:
        try:
            CapabilityResolver.require(self.provider, "tool_calling")
        except UnsupportedCapabilityError:
            return AgentDecision(
                error={
                    "code": "unsupported_capability",
                    "message": (
                        "The configured LLM does not support Structured Data tool calling."
                    ),
                }
            )
        base_url = getattr(self.provider, "base_url", "")
        api_key = getattr(self.provider, "api_key", "")
        model = getattr(self.provider, "model", "")
        if not base_url or (
            not api_key and getattr(self.provider, "provider_name", "") == "remote"
        ):
            return AgentDecision(
                error={
                    "code": "llm_unavailable",
                    "message": (
                        "The configured LLM is unavailable until credentials are configured."
                    ),
                }
            )
        descriptors = describe_tools(self.settings, tool_ids)
        payload: dict[str, Any] = {
            "model": model,
            "messages": build_agent_messages(
                state=state, descriptors=descriptors, tool_results=tool_results
            ),
            "stream": False,
        }
        if remaining_iterations > 0 and descriptors:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": descriptor.tool_id,
                        "description": descriptor.description,
                        "parameters": descriptor.schema,
                    },
                }
                for descriptor in descriptors
            ]
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            response = httpx.post(
                f"{str(base_url).rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            logger.warning("sql_agent_model_failed: %s", type(error).__name__)
            return AgentDecision(
                error={
                    "code": "llm_unavailable",
                    "message": "The configured LLM could not complete the request safely.",
                }
            )
        calls: list[ToolCallProposal] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_id = str(function.get("name", ""))
            if tool_id not in tool_ids:
                continue
            calls.append(
                ToolCallProposal(
                    tool_id=tool_id,
                    tool_call_id=str(call.get("id") or f"{state['run_id']}-call-{index}"),
                    arguments=_parse_arguments(function.get("arguments")),
                )
            )
        content = message.get("content")
        if calls:
            return AgentDecision(tool_calls=tuple(calls))
        if isinstance(content, str) and content.strip():
            return AgentDecision(answer=content.strip())
        return AgentDecision(
            error={
                "code": "empty_model_response",
                "message": "The Structured Data agent did not produce a usable response.",
            }
        )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class ScriptedSQLAgentModel:
    """Deterministic model for tests and offline smoke runs.

    It never invents data: it asks for schema or a bounded query and then reports
    the received Tool Results.
    """

    def decide(
        self,
        *,
        state: GraphState,
        tool_ids: tuple[str, ...],
        tool_results: list[dict[str, Any]],
        remaining_iterations: int,
    ) -> AgentDecision:
        if remaining_iterations <= 0:
            return AgentDecision(
                answer="The Structured Data agent reached its Tool iteration limit."
            )
        lowered = state["user_input"].casefold()
        bound_query_result_id = _bound_query_result_id(state)
        if bound_query_result_id and "data_export" in tool_ids and not tool_results:
            # Resume path: the approved proposal already bound the snapshot.
            return AgentDecision(
                tool_calls=(
                    ToolCallProposal(
                        tool_id="data_export",
                        tool_call_id=f"{state['run_id']}-export",
                        arguments={
                            "query_result_id": bound_query_result_id,
                            "format": "csv",
                            "destination": "managed_export_area",
                            "reason": "Requested by the user from the conversation.",
                        },
                    ),
                )
            )
        successful_query = next(
            (
                result
                for result in tool_results
                if result.get("tool_id") == "database_query" and result.get("status") == "success"
            ),
            None,
        )
        export_requested = any(term in lowered for term in ("export", "csv", "导出", "download"))
        if (
            export_requested
            and successful_query is not None
            and "data_export" in tool_ids
            and not any(result.get("tool_id") == "data_export" for result in tool_results)
        ):
            query_result_id = (successful_query.get("data") or {}).get("query_result_id")
            return AgentDecision(
                tool_calls=(
                    ToolCallProposal(
                        tool_id="data_export",
                        tool_call_id=f"{state['run_id']}-export",
                        arguments={
                            "query_result_id": query_result_id,
                            "format": "csv",
                            "destination": "managed_export_area",
                            "reason": "Requested by the user from the conversation.",
                        },
                    ),
                )
            )
        if not tool_results:
            if "schema" in lowered and "database_schema" in tool_ids:
                return AgentDecision(
                    tool_calls=(
                        ToolCallProposal(
                            tool_id="database_schema",
                            tool_call_id=f"{state['run_id']}-schema",
                            arguments={},
                        ),
                    )
                )
            if "drop" in lowered or "delete" in lowered or "unsafe" in lowered:
                sql = "DELETE FROM sales_orders"
            else:
                sql = (
                    "SELECT order_id, region, amount FROM sales_orders ORDER BY amount DESC LIMIT 5"
                )
            return AgentDecision(
                tool_calls=(
                    ToolCallProposal(
                        tool_id="database_query",
                        tool_call_id=f"{state['run_id']}-query",
                        arguments={"sql": sql, "max_rows": 5},
                    ),
                )
            )
        last = tool_results[-1]
        if last.get("tool_id") == "data_export":
            if last.get("status") == "success":
                data = last.get("data") or {}
                return AgentDecision(
                    answer=(
                        f"The CSV export completed with {data.get('row_count', 0)} rows "
                        f"under approval {data.get('approval_request_id')}."
                    )
                )
            error = (last.get("error") or {}).get("code")
            if error == "approval_required":
                # The Run pauses for the approval decision; the successor Run resumes here.
                return AgentDecision(
                    answer="Export approval is required before the file can be produced."
                )
            return AgentDecision(answer=f"The export could not be completed: {error}.")
        if last.get("status") == "success" and last.get("tool_id") == "database_query":
            data = last.get("data") or {}
            return AgentDecision(
                answer=(
                    f"The query returned {data.get('row_count', 0)} rows"
                    f"{' (truncated)' if data.get('truncated') else ''}."
                )
            )
        if last.get("status") == "success" and last.get("tool_id") == "database_schema":
            tables = (last.get("data") or {}).get("tables") or []
            return AgentDecision(answer=f"The allowlisted schema contains {len(tables)} tables.")
        error = (last.get("error") or {}).get("code", "tool_error")
        return AgentDecision(answer=f"The Structured Data request could not be completed: {error}.")


def create_sql_agent_model(provider: Any, settings: Settings) -> SQLAgentModel:
    """Select the Structured Data Agent model.

    ``sql_agent_provider`` defaults to ``auto``: the deterministic scripted model runs
    for the offline development LLM profiles, and the LLM tool-calling model runs for
    the remote/local profiles. ``llm`` forces the LLM model even when the provider
    lacks tool calling, which then fails safely with ``unsupported_capability``.
    """

    mode = getattr(settings, "sql_agent_provider", "auto")
    if mode == "scripted":
        return ScriptedSQLAgentModel()
    if mode == "llm":
        return LLMSQLAgentModel(provider=provider, settings=settings)
    if settings.llm_provider in {"mock", "extractive"} or isinstance(provider, MockGeneralLLM):
        return ScriptedSQLAgentModel()
    return LLMSQLAgentModel(provider=provider, settings=settings)
