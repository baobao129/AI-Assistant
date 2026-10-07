from typing import Literal, TypedDict

Intent = Literal["knowledge", "structured_data", "general"]
RoutingOutcome = Literal["routed", "unsupported_mixed_query", "invalid_request", "routing_error"]
CurrentPath = Literal["knowledge", "structured_data", "general"]
InterruptStatus = Literal["none", "pending", "approved", "rejected", "expired"]


class GraphState(TypedDict):
    """Bounded execution state; Conversation Persistence and Events stay outside this type."""

    conversation_id: str
    thread_id: str
    run_id: str
    user_input: str
    intent: Intent | None
    routing_outcome: RoutingOutcome | None
    current_path: CurrentPath | None
    messages: list[dict[str, object]]
    tool_calls: list[dict[str, object]]
    tool_results: list[dict[str, object]]
    retrieval_query: str | None
    retrieval_results: list[dict[str, object]]
    context: str | None
    query_result_id: str | None
    sql_validation: dict[str, object] | None
    answer_candidate: str | None
    answer_validation: dict[str, object] | None
    # Candidate-local Answer Validation revision counter owned by the validation
    # policy (GRAPH_CONTRACT Graph-008 / Graph-005 loop limits).
    answer_revision_count: int
    citations: list[dict[str, object]]
    approval_request_id: str | None
    interrupt_status: InterruptStatus
    error: dict[str, object] | None
    final_response: str | None


def initial_state(
    *, conversation_id: str, thread_id: str, run_id: str, user_input: str
) -> GraphState:
    return {
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "user_input": user_input,
        "intent": None,
        "routing_outcome": None,
        "current_path": None,
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
        "retrieval_query": None,
        "retrieval_results": [],
        "context": None,
        "query_result_id": None,
        "sql_validation": None,
        "answer_candidate": None,
        "answer_validation": None,
        "answer_revision_count": 0,
        "citations": [],
        "approval_request_id": None,
        "interrupt_status": "none",
        "error": None,
        "final_response": None,
    }
