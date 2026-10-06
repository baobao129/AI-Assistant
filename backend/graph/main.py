from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from backend.domain.state import GraphState
from backend.graph.policies import ExecutionBudgets
from backend.graph.validation import validate_answer
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


def request_initialize(state: GraphState) -> dict[str, object]:
    return {
        "intent": None,
        "routing_outcome": None,
        "current_path": None,
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
        "retrieval_results": [],
        "citations": [],
        "answer_candidate": None,
        "answer_validation": None,
        "approval_request_id": None,
        "interrupt_status": "none",
        "error": None,
        "final_response": None,
    }


def intent_router(state: GraphState) -> dict[str, object]:
    query = state["user_input"].strip()
    lowered = query.casefold()
    if not query:
        return {"intent": None, "routing_outcome": "invalid_request", "current_path": None}
    if "__routing_error__" in lowered:
        return {"intent": None, "routing_outcome": "routing_error", "current_path": None}
    knowledge = any(term in lowered for term in KNOWLEDGE_TERMS)
    structured = any(term in lowered for term in STRUCTURED_TERMS)
    if knowledge and structured:
        return {"intent": None, "routing_outcome": "unsupported_mixed_query", "current_path": None}
    if knowledge:
        return {"intent": "knowledge", "routing_outcome": "routed", "current_path": "knowledge"}
    if structured:
        return {
            "intent": "structured_data",
            "routing_outcome": "routed",
            "current_path": "structured_data",
        }
    return {"intent": "general", "routing_outcome": "routed", "current_path": "general"}


def retrieval(state: GraphState, executor: ToolExecutor) -> dict[str, object]:
    result = executor.invoke(
        "knowledge_search", "knowledge-search-1", {"query": state["user_input"], "top_k": 5}
    )
    if result.status != "success":
        return {
            "error": result.error.model_dump() if result.error else {"code": "retrieval_failed"}
        }
    data = result.data
    return {
        "retrieval_query": data.get("query"),
        "retrieval_results": data.get("results", []),
        "citations": [item["citation"] for item in data.get("results", []) if "citation" in item],
    }


def parent_child_mapping(state: GraphState) -> dict[str, object]:
    # The stub preserves already-normalized parent references for the future adapter.
    return {"retrieval_results": state.get("retrieval_results", [])[:5]}


def context_build(state: GraphState) -> dict[str, object]:
    excerpts = [str(item.get("content", ""))[:1000] for item in state.get("retrieval_results", [])]
    return {"context": "\n\n".join(excerpts)[:5000] if excerpts else None}


def generation(state: GraphState) -> dict[str, object]:
    if not state.get("context"):
        answer = "Insufficient evidence is available in the authorized knowledge sources."
    else:
        answer = "The answer is grounded in the retrieved authorized context."
    return {"answer_candidate": answer, "messages": [{"role": "assistant", "content": answer}]}


def general_chat(state: GraphState) -> dict[str, object]:
    answer = f"General response: {state['user_input'].strip()}"
    return {"answer_candidate": answer, "messages": [{"role": "assistant", "content": answer}]}


def sql_model_node(state: GraphState) -> dict[str, object]:
    if state.get("tool_results"):
        answer = (
            "Structured data tool results were received; the bounded skeleton "
            "has no live data source."
        )
        return {
            "tool_calls": [],
            "answer_candidate": answer,
            "messages": [{"role": "assistant", "content": answer}],
        }
    lowered = state["user_input"].casefold()
    if "schema" in lowered:
        call = {"tool_id": "database_schema", "tool_call_id": "schema-1", "arguments": {}}
    else:
        sql = "SELECT 1"
        if "drop" in lowered or "delete" in lowered or "outfile" in lowered:
            sql = "DROP TABLE restricted_table"
        call = {
            "tool_id": "database_query",
            "tool_call_id": "query-1",
            "arguments": {"sql": sql, "max_rows": 100},
        }
    return {"tool_calls": [call], "messages": [{"role": "assistant", "tool_calls": [call]}]}


def tools_condition(state: GraphState, budgets: ExecutionBudgets | None = None) -> str:
    """Conditional edge only; it does not execute Tools or mutate State."""
    limits = budgets or ExecutionBudgets()
    if state.get("tool_calls") and limits.can_continue_tools(len(state.get("tool_results", []))):
        return "tool_executor"
    return "sql_done"


def tool_executor(state: GraphState, executor: ToolExecutor) -> dict[str, object]:
    call = state.get("tool_calls", [])[0]
    result = executor.invoke(call["tool_id"], call["tool_call_id"], call.get("arguments", {}))
    return {"tool_results": [result.model_dump()]}


def tool_result_normalize(state: GraphState) -> dict[str, object]:
    result = state.get("tool_results", [])[-1]
    error = result.get("error") or {}
    update: dict[str, object] = {
        "tool_calls": [],
        "messages": [{"role": "tool", "tool_call_id": result["tool_call_id"], "content": result}],
    }
    data = result.get("data", {})
    if result.get("tool_id") == "database_query" and result.get("status") == "success":
        update["query_result_id"] = data.get("query_result_id")
        update["sql_validation"] = {"status": "accepted", "reasons": []}
    elif result.get("tool_id") == "database_query":
        update["sql_validation"] = {
            "status": "rejected",
            "reasons": [error.get("code", "sql_rejected")],
        }
    if error.get("code") == "approval_required":
        update["approval_request_id"] = data.get("approval_request_id")
        update["interrupt_status"] = "pending"
    return update


def response_assembly(state: GraphState) -> dict[str, object]:
    if state.get("answer_candidate"):
        return {}
    if state.get("routing_outcome") == "unsupported_mixed_query":
        return {
            "answer_candidate": (
                "This request combines Knowledge and Structured Data; "
                "that path is unsupported in the MVP."
            )
        }
    if state.get("routing_outcome") == "invalid_request":
        return {"answer_candidate": "Please provide a non-empty request."}
    if state.get("routing_outcome") == "routing_error":
        return {"answer_candidate": "The request could not be routed safely."}
    return {"answer_candidate": "The selected path completed without a response candidate."}


def safe_response(state: GraphState) -> dict[str, object]:
    return response_assembly(state)


def response_finalize(state: GraphState) -> dict[str, object]:
    answer = state.get("answer_candidate") or "The request could not be completed safely."
    validation = state.get("answer_validation")
    if validation and validation.get("status") == "insufficient_evidence":
        answer = "Insufficient evidence is available in the authorized knowledge sources."
    return {"final_response": answer}


def _route_main(state: GraphState) -> str:
    outcome = state.get("routing_outcome")
    if outcome != "routed":
        return "safe_response"
    return state["current_path"] or "safe_response"


def _build_rag_nodes(
    executor: ToolExecutor,
) -> dict[str, Callable[[GraphState], dict[str, object]]]:
    return {
        "retrieval": lambda state: retrieval(state, executor),
        "parent_child_mapping": parent_child_mapping,
        "context_build": context_build,
        "generation": generation,
    }


def build_main_graph(
    executor: ToolExecutor | None = None,
    checkpointer: object | None = None,
    budgets: ExecutionBudgets | None = None,
):
    from backend.tools.mock_tools import default_registry

    tool_executor = executor or ToolExecutor(default_registry())
    limits = budgets or ExecutionBudgets()
    graph = StateGraph(GraphState)
    graph.add_node("request_initialize", request_initialize)
    graph.add_node("intent_router", intent_router)
    graph.add_node("safe_response", safe_response)
    graph.add_node("response_assembly", response_assembly)
    graph.add_node("answer_validation", validate_answer)
    graph.add_node("response_finalize", response_finalize)
    graph.add_node("general_chat", general_chat)
    for name, node in _build_rag_nodes(tool_executor).items():
        graph.add_node(name, node)
    graph.add_node("model_node", sql_model_node)
    graph.add_node("tool_executor", lambda state: tool_executor_node(state, tool_executor))
    graph.add_node("tool_result_normalize", tool_result_normalize)
    graph.add_node("sql_done", lambda state: {})

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
    graph.add_edge("answer_validation", "response_finalize")
    graph.add_edge("response_finalize", END)
    return graph.compile(checkpointer=checkpointer)


def tool_executor_node(state: GraphState, executor: ToolExecutor) -> dict[str, object]:
    return tool_executor(state, executor)
