from collections.abc import Iterable
from typing import Annotated, Literal, TypedDict

Intent = Literal["knowledge", "structured_data", "general"]
RoutingOutcome = Literal["routed", "unsupported_mixed_query", "invalid_request", "routing_error"]
CurrentPath = Literal["knowledge", "structured_data", "general"]
InterruptStatus = Literal["none", "pending", "approved", "rejected", "expired"]

# 有界追加 reducer：State 集合在合并时保留「最新 N 条」，既满足
# GRAPH_CONTRACT Graph-006 的 append 语义（tool_results 必须累加、messages 是模型历史），
# 又满足 STATE_CONTRACT 的「集合必须有界」要求。上限均大于单次 Run 的最大工具循环量。
MESSAGES_LIMIT = 40
TOOL_RESULTS_LIMIT = 24

# 请求级文档作用域的硬上限（ADR-039 / CC-STATE-004）。API 请求字段、Graph State 与
# knowledge_search 的输入上限共用同一个常量，避免三处各自漂移。
MAX_DOCUMENT_SCOPE_IDS = 20


def bounded_document_scope(document_ids: Iterable[str] | None) -> list[str]:
    """把作用域去重并截断到硬上限，保留首次出现顺序（State 集合必须有界）。"""

    if not document_ids:
        return []
    unique: list[str] = []
    for item in document_ids:
        token = str(item)
        if token and token not in unique:
            unique.append(token)
        if len(unique) >= MAX_DOCUMENT_SCOPE_IDS:
            break
    return unique


def bounded_add(limit: int):
    """返回一个「追加但保留最新 limit 条」的 LangGraph reducer。"""

    def reducer(left: list | None, right: list | None) -> list:
        merged = list(left or []) + list(right or [])
        return merged[-limit:]

    return reducer


class GraphState(TypedDict):
    """有界的执行状态；Conversation Persistence 与 Events 不在此类型内。"""

    conversation_id: str
    thread_id: str
    run_id: str
    user_input: str
    intent: Intent | None
    routing_outcome: RoutingOutcome | None
    current_path: CurrentPath | None
    # 请求级文档作用域（ADR-039）：服务端校验后的授权集合子集，只能收窄检索范围。
    # 由 request initializer 写入一次；``request_initialize`` **不得**重置它，因此
    # HITL Resume 与审批 successor 会从 Checkpoint 继承，而不是退回「无作用域」。
    document_scope: list[str]
    messages: Annotated[list[dict[str, object]], bounded_add(MESSAGES_LIMIT)]
    # tool_calls 保持「替换」语义：tool_result_normalize 依赖返回 [] 清空当前建议，
    # 追加 reducer 会让清空失效并把循环重新路由回 tool_executor。
    tool_calls: list[dict[str, object]]
    tool_results: Annotated[list[dict[str, object]], bounded_add(TOOL_RESULTS_LIMIT)]
    retrieval_query: str | None
    retrieval_results: list[dict[str, object]]
    context: str | None
    query_result_id: str | None
    sql_validation: dict[str, object] | None
    answer_candidate: str | None
    answer_validation: dict[str, object] | None
    # 候选级 Answer Validation revision 计数器，由 validation policy 拥有
    # （GRAPH_CONTRACT Graph-008 / Graph-005 的循环上限）。
    answer_revision_count: int
    citations: list[dict[str, object]]
    approval_request_id: str | None
    interrupt_status: InterruptStatus
    error: dict[str, object] | None
    final_response: str | None


def initial_state(
    *,
    conversation_id: str,
    thread_id: str,
    run_id: str,
    user_input: str,
    document_scope: Iterable[str] | None = None,
) -> GraphState:
    return {
        "conversation_id": conversation_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "user_input": user_input,
        "intent": None,
        "routing_outcome": None,
        "current_path": None,
        "document_scope": bounded_document_scope(document_scope),
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
