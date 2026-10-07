"""Main Graph topology per ``docs/contracts/GRAPH_CONTRACT.md``.

Every legal path terminates in a final, safe error, rejection, or unsupported
outcome. Nodes return bounded partial State updates; Tools are reached only through
the Tool Registry and Tool Executor.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from backend.domain.state import GraphState
from backend.graph.agent_model import SQLAgentModel, create_sql_agent_model
from backend.graph.policies import ExecutionBudgets
from backend.graph.validation import (
    AnswerValidator,
    revision_update,
    should_revise,
    validate_answer,
)
from backend.infrastructure.config import Settings
from backend.infrastructure.llm import (
    GeneralLLM,
    LLMProvider,
    MockGeneralLLM,
    create_llm_provider,
)
from backend.tools.contracts import ToolExecutionContext, ToolResult
from backend.tools.registry import ToolExecutor

KNOWLEDGE_TERMS = ("document", "policy", "manual", "contract", "knowledge", "文档", "政策", "手册")
STRUCTURED_TERMS = (
    "sales",
    "procurement",
    "purchase",
    "revenue",
    "amount",
    "count",
    "table",
    "sql",
    "销售",
    "采购",
    "金额",
    "数量",
)

STRUCTURED_TOOL_IDS = ("database_schema", "database_query", "data_export")
RAG_TOOL_IDS = ("knowledge_search",)


class ToolInvoker(Protocol):
    """Invocation boundary used by Graph nodes; it never receives Graph State."""

    def invoke(
        self,
        tool_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult: ...


class ScopedToolInvoker:
    """Binds one execution scope to the Tool Executor for a single Graph Run."""

    def __init__(self, executor: ToolExecutor, context: ToolExecutionContext) -> None:
        self.executor = executor
        self.context = context

    def for_approval(self, approval_request_id: str | None) -> ScopedToolInvoker:
        return ScopedToolInvoker(
            self.executor,
            ToolExecutionContext(
                conversation_id=self.context.conversation_id,
                thread_id=self.context.thread_id,
                run_id=self.context.run_id,
                user_id=self.context.user_id,
                approval_request_id=approval_request_id,
            ),
        )

    def invoke(
        self,
        tool_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        return self.executor.invoke(tool_id, tool_call_id, arguments, context or self.context)


class IntentRouter:
    """Deterministic first-pass classifier with explicit safe Routing Outcomes."""

    def route(self, user_input: str) -> dict[str, object]:
        query = user_input.strip()
        lowered = query.casefold()
        if not query:
            return {"intent": None, "routing_outcome": "invalid_request", "current_path": None}
        if "__routing_error__" in lowered:
            return {"intent": None, "routing_outcome": "routing_error", "current_path": None}
        knowledge = any(term in lowered for term in KNOWLEDGE_TERMS)
        structured = any(term in lowered for term in STRUCTURED_TERMS)
        if knowledge and structured:
            return {
                "intent": None,
                "routing_outcome": "unsupported_mixed_query",
                "current_path": None,
            }
        if knowledge:
            return {"intent": "knowledge", "routing_outcome": "routed", "current_path": "knowledge"}
        if structured:
            return {
                "intent": "structured_data",
                "routing_outcome": "routed",
                "current_path": "structured_data",
            }
        return {"intent": "general", "routing_outcome": "routed", "current_path": "general"}

    classify = route


def request_initialize(state: GraphState) -> dict[str, object]:
    return {
        "intent": None,
        "routing_outcome": None,
        "current_path": None,
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
        "retrieval_results": [],
        "context": None,
        "citations": [],
        "answer_candidate": None,
        "answer_validation": None,
        "answer_revision_count": 0,
        "error": None,
        "final_response": None,
    }


def intent_router(state: GraphState, router: IntentRouter | None = None) -> dict[str, object]:
    return (router or IntentRouter()).route(state["user_input"])


class RAGNodes:
    """Dense Retrieval -> Parent-Child Mapping -> Context Build -> Generation."""

    def __init__(
        self,
        *,
        tools: ToolInvoker,
        settings: Settings,
        general_llm: GeneralLLM,
        revision_provider: Callable[[], GeneralLLM] | None = None,
    ) -> None:
        self.tools = tools
        self.settings = settings
        self.general_llm = general_llm

    def retrieval(self, state: GraphState) -> dict[str, object]:
        result = self.tools.invoke(
            "knowledge_search",
            f"{state['run_id']}-knowledge-1",
            {
                "query": state["user_input"],
                "top_k": self.settings.default_retrieval_top_k,
            },
        )
        if result.status != "success":
            code = result.error.code if result.error else "retrieval_failed"
            return {
                "error": {
                    "code": code,
                    "message": result.error.message if result.error else "Retrieval failed.",
                }
            }
        data = result.data or {}
        results = list(data.get("results") or [])[: self.settings.max_retrieval_top_k]
        return {
            "retrieval_query": str(data.get("query") or state["user_input"]),
            "retrieval_results": results,
            "citations": [item["citation"] for item in results if "citation" in item][
                : self.settings.max_citations
            ],
        }

    def parent_child_mapping(self, state: GraphState) -> dict[str, object]:
        mapped = [
            item
            for item in state.get("retrieval_results", [])
            if item.get("parent_chunk_id") or item.get("content")
        ]
        return {"retrieval_results": mapped[: self.settings.max_retrieval_top_k]}

    def context_build(self, state: GraphState) -> dict[str, object]:
        sections: list[str] = []
        used = 0
        for item in state.get("retrieval_results", []):
            label = str(item.get("metadata", {}).get("label") or item.get("chunk_id") or "source")
            body = str(item.get("content") or "").strip()
            if not body:
                continue
            section = f"[{label}] {body}"
            if used + len(section) > self.settings.max_context_chars:
                sections.append(section[: max(0, self.settings.max_context_chars - used)])
                break
            sections.append(section)
            used += len(section)
        return {"context": "\n\n".join(sections) if sections else None}

    def generation(self, state: GraphState) -> dict[str, object]:
        if not state.get("context"):
            # No authorized evidence: never fill enterprise facts from pretrained knowledge.
            return {
                "answer_candidate": (
                    "Insufficient evidence is available in the authorized knowledge sources "
                    "to answer this question."
                ),
                "messages": [{"role": "assistant", "content": "insufficient_evidence"}],
            }
        prompt = (
            "Answer strictly from the provided authorized context. Cite the bracketed labels "
            "you used. If the context does not contain the answer, reply with "
            "'insufficient evidence' and nothing else.\n\n"
            f"Context:\n{state['context']}\n\nQuestion: {state['user_input']}"
        )
        try:
            answer = self.general_llm.generate(prompt, state)
        except Exception:  # noqa: BLE001 - provider failures become a safe outcome
            answer = "The configured LLM could not complete the request safely."
        return {"answer_candidate": answer, "messages": [{"role": "assistant", "content": answer}]}


def general_chat(state: GraphState, llm: GeneralLLM, settings: Settings) -> dict[str, object]:
    prompt = (
        "You are a general internal assistant. Answer the user's general question directly. "
        "You cannot access enterprise documents or business data on this path.\n\n"
        f"Question: {state['user_input']}"
    )
    try:
        answer = llm.generate(prompt[: settings.llm_max_input_chars], state)
    except Exception:  # noqa: BLE001
        answer = "The configured LLM could not complete the request safely."
    return {"answer_candidate": answer, "messages": [{"role": "assistant", "content": answer}]}


def sql_model_node(
    state: GraphState,
    *,
    agent: SQLAgentModel,
    tool_ids: tuple[str, ...],
    budgets: ExecutionBudgets,
) -> dict[str, object]:
    iterations = len(state.get("tool_results", []))
    decision = agent.decide(
        state=state,
        tool_ids=tool_ids,
        tool_results=list(state.get("tool_results", [])),
        remaining_iterations=max(0, budgets.max_tool_iterations - iterations),
    )
    if decision.error is not None:
        return {"tool_calls": [], "error": decision.error}
    if decision.tool_calls:
        calls = [
            {
                "tool_id": proposal.tool_id,
                "tool_call_id": proposal.tool_call_id,
                "arguments": proposal.arguments,
            }
            for proposal in decision.tool_calls
        ]
        return {
            "tool_calls": calls,
            "messages": [{"role": "assistant", "tool_calls": calls}],
        }
    answer = decision.answer or "The Structured Data path produced no answer."
    return {
        "tool_calls": [],
        "answer_candidate": answer,
        "messages": [{"role": "assistant", "content": answer}],
    }


def tools_condition(state: GraphState, budgets: ExecutionBudgets | None = None) -> str:
    """Conditional edge only; it does not execute Tools or mutate State."""

    limits = budgets or ExecutionBudgets()
    if state.get("tool_calls") and limits.can_continue_tools(len(state.get("tool_results", []))):
        return "tool_executor"
    return "sql_done"


def tool_executor_node(
    state: GraphState, *, tools: ToolInvoker, budgets: ExecutionBudgets
) -> dict[str, object]:
    calls = state.get("tool_calls", [])
    if not calls:
        return {}
    pending = calls[0]
    result = tools.invoke(
        str(pending["tool_id"]),
        str(pending["tool_call_id"]),
        dict(pending.get("arguments") or {}),
    )
    return {"tool_results": [result.model_dump()]}


def tool_result_normalize(state: GraphState) -> dict[str, object]:
    """Maps an already-produced normalized Tool Result into declared State fields."""

    results = state.get("tool_results", [])
    if not results:
        return {}
    result = results[-1]
    update: dict[str, object] = {
        "tool_calls": [],
        "messages": [
            {
                "role": "tool",
                "tool_call_id": result.get("tool_call_id"),
                "content": {
                    "tool_id": result.get("tool_id"),
                    "status": result.get("status"),
                    "data": result.get("data"),
                    "error": result.get("error"),
                },
            }
        ],
    }
    data = result.get("data") or {}
    error = result.get("error") or {}
    if result.get("tool_id") == "database_query":
        if result.get("status") == "success":
            update["query_result_id"] = data.get("query_result_id")
            update["sql_validation"] = {"status": "accepted", "reasons": []}
        else:
            update["sql_validation"] = {
                "status": "rejected",
                "reasons": list(
                    (result.get("metadata") or {}).get("reasons") or [error.get("code")]
                ),
            }
    if result.get("tool_id") == "data_export" and error.get("code") == "approval_required":
        update["approval_request_id"] = data.get("approval_request_id")
        update["interrupt_status"] = "pending"
    return update


def response_assembly(state: GraphState) -> dict[str, object]:
    if state.get("answer_candidate"):
        return {}
    outcome = state.get("routing_outcome")
    if outcome == "unsupported_mixed_query":
        return {
            "answer_candidate": (
                "This request combines Knowledge and Structured Data. Mixed-source requests "
                "are unsupported; please ask about one source at a time."
            )
        }
    if outcome == "invalid_request":
        return {"answer_candidate": "Please provide a non-empty request."}
    if outcome == "routing_error":
        return {"answer_candidate": "The request could not be routed safely."}
    error = state.get("error") or {}
    if error:
        return {
            "answer_candidate": str(
                error.get("message") or "The selected path could not complete safely."
            )
        }
    if state.get("tool_results"):
        return {"answer_candidate": "Structured Data tool results were received."}
    return {"answer_candidate": "The selected path completed without a response candidate."}


def safe_response(state: GraphState) -> dict[str, object]:
    return response_assembly(state)


def response_finalize(state: GraphState) -> dict[str, object]:
    answer = state.get("answer_candidate") or "The request could not be completed safely."
    validation = state.get("answer_validation") or {}
    status = validation.get("status")
    if status == "insufficient_evidence":
        answer = (
            "Insufficient evidence is available in the authorized knowledge sources "
            "to answer this question."
        )
    elif status == "invalid_result":
        answer = "The answer could not be validated against the available evidence."
    elif status == "validation_error":
        answer = "The answer could not be validated safely."
    return {"final_response": answer[:20000]}


def _route_main(state: GraphState) -> str:
    outcome = state.get("routing_outcome")
    if outcome != "routed":
        return "safe_response"
    return state.get("current_path") or "safe_response"


def _route_validation(state: GraphState, budgets: ExecutionBudgets) -> str:
    """One bounded revision attempt, otherwise finalize with the validation outcome."""

    if should_revise(state, budgets):
        return "revise_knowledge" if state.get("intent") == "knowledge" else "revise_structured"
    return "response_finalize"


def build_main_graph(
    executor: ToolInvoker | None = None,
    checkpointer: object | None = None,
    budgets: ExecutionBudgets | None = None,
    general_llm: GeneralLLM | None = None,
    settings: Settings | None = None,
    *,
    context: ToolExecutionContext | None = None,
    sql_agent: SQLAgentModel | None = None,
    validator: AnswerValidator | None = None,
):
    config = settings or Settings(_env_file=None)
    if executor is None:
        # Development default: the stub registry keeps the skeleton testable without
        # infrastructure. The runtime always injects the real scoped Tool Executor.
        from backend.tools.mock_tools import default_registry

        executor = ToolExecutor(default_registry())
    tools: ToolInvoker = executor
    if isinstance(executor, ToolExecutor):
        tools = ScopedToolInvoker(executor, context or ToolExecutionContext())
    limits = budgets or ExecutionBudgets(
        max_tool_iterations=config.max_tool_iterations,
        max_repair_attempts=config.max_repair_attempts,
        max_answer_revisions=config.max_answer_revisions,
    )
    provider = general_llm or (create_llm_provider(config) if settings else MockGeneralLLM())
    agent = sql_agent or create_sql_agent_model(provider, config)
    rag = RAGNodes(tools=tools, settings=config, general_llm=provider)
    answer_validator = validator or validate_answer

    graph = StateGraph(GraphState)
    graph.add_node("request_initialize", request_initialize)
    graph.add_node("intent_router", intent_router)
    graph.add_node("safe_response", safe_response)
    graph.add_node("response_assembly", response_assembly)
    graph.add_node("answer_validation", lambda state: answer_validator(state, limits))
    graph.add_node("response_finalize", response_finalize)
    graph.add_node("general_chat", lambda state: general_chat(state, provider, config))
    graph.add_node("retrieval", rag.retrieval)
    graph.add_node("parent_child_mapping", rag.parent_child_mapping)
    graph.add_node("context_build", rag.context_build)
    graph.add_node("generation", rag.generation)
    graph.add_node(
        "model_node",
        lambda state: sql_model_node(state, agent=agent, tool_ids=_tool_ids(tools), budgets=limits),
    )
    graph.add_node(
        "tool_executor",
        lambda state: tool_executor_node(state, tools=tools, budgets=limits),
    )
    graph.add_node("tool_result_normalize", tool_result_normalize)
    graph.add_node("sql_done", lambda state: {})
    graph.add_node("revision_guard", revision_update)

    graph.add_edge(START, "request_initialize")
    graph.add_edge("request_initialize", "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        _route_main,
        {
            "knowledge": "retrieval",
            "structured_data": "model_node",
            "general": "general_chat",
            "safe_response": "safe_response",
        },
    )
    graph.add_edge("retrieval", "parent_child_mapping")
    graph.add_edge("parent_child_mapping", "context_build")
    graph.add_edge("context_build", "generation")
    graph.add_edge("generation", "response_assembly")
    graph.add_edge("general_chat", "response_assembly")
    graph.add_conditional_edges(
        "model_node",
        lambda state: tools_condition(state, limits),
        {"tool_executor": "tool_executor", "sql_done": "sql_done"},
    )
    graph.add_edge("tool_executor", "tool_result_normalize")
    graph.add_edge("tool_result_normalize", "model_node")
    graph.add_edge("sql_done", "response_assembly")
    graph.add_edge("safe_response", "response_finalize")
    graph.add_edge("response_assembly", "answer_validation")
    graph.add_conditional_edges(
        "answer_validation",
        lambda state: _route_validation(state, limits),
        {
            "revise_knowledge": "revision_guard",
            "revise_structured": "revision_guard",
            "response_finalize": "response_finalize",
        },
    )
    graph.add_conditional_edges(
        "revision_guard",
        lambda state: "knowledge" if state.get("intent") == "knowledge" else "structured_data",
        {"knowledge": "generation", "structured_data": "model_node"},
    )
    graph.add_edge("response_finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _tool_ids(tools: ToolInvoker) -> tuple[str, ...]:
    registry = getattr(getattr(tools, "executor", tools), "registry", None)
    if registry is None:
        return STRUCTURED_TOOL_IDS
    available = set(registry.list_tool_ids())
    return tuple(tool_id for tool_id in STRUCTURED_TOOL_IDS if tool_id in available)


__all__ = [
    "IntentRouter",
    "RAGNodes",
    "ScopedToolInvoker",
    "ToolInvoker",
    "build_main_graph",
    "general_chat",
    "intent_router",
    "request_initialize",
    "response_assembly",
    "response_finalize",
    "safe_response",
    "sql_model_node",
    "tool_executor_node",
    "tool_result_normalize",
    "tools_condition",
    "LLMProvider",
]
