"""依据 ``docs/contracts/GRAPH_CONTRACT.md`` 的主 Graph 拓扑。

每条合法路径都以 final、安全 error、rejection 或 unsupported 结果终止。节点返回
有界的部分 State 更新；Tool 只能经 Tool Registry 与 Tool Executor 访问。
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

# 属于瞬时失败、可进入有界 Graph repair 预算（`max_repair_attempts`）的错误码。
# 安全拒绝、权限失败、超时与已完成的副作用被刻意排除：它们是终态，绝不修复或
# 重试（GRAPH_CONTRACT Graph-009）。瞬时 Tool 失败由所属 Tool 的重试策略处理，
# 不走这条 Graph repair 路径。
RECOVERABLE_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "llm_unavailable",
        "retrieval_unavailable",
    }
)


def failure(
    code: str,
    message: str,
    *,
    attempts: int = 0,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造承载在 Graph State 中的有界错误信封。

    ``repair_attempts`` 是 Graph/Application repair 预算的计数器；新的 Answer
    Validation 候选不会重置它，Resume 也会保留它。
    """

    return {
        "code": code,
        "message": message,
        "recoverable": code in RECOVERABLE_FAILURE_CODES,
        "repair_attempts": attempts,
        "details": details or {},
    }


# retrieval Tool 上报的 provider/store 失败码被归一为一个稳定的、可修复错误码；
# 原始错误码保留在有界的 `details` 中。
TRANSIENT_RETRIEVAL_CODES: frozenset[str] = frozenset(
    {
        "embedding_provider_unavailable",
        "embedding_provider_missing_credentials",
        "embedding_provider_returned_empty_vector",
        "embedding_provider_returned_no_vector",
        "embedding_provider_returned_wrong_count",
        "vector_store_unavailable",
        "vector_store_write_failed",
        "vector_verification_failed",
    }
)


def is_recoverable(state: GraphState) -> bool:
    error = state.get("error") or {}
    return bool(error.get("recoverable")) and str(error.get("code")) in RECOVERABLE_FAILURE_CODES


def repair_count(state: GraphState) -> int:
    error = state.get("error") or {}
    try:
        return max(0, int(error.get("repair_attempts") or 0))
    except (TypeError, ValueError):
        return 0


class ToolInvoker(Protocol):
    """Graph 节点使用的调用边界；它绝不接收 Graph State。"""

    def invoke(
        self,
        tool_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult: ...


class ScopedToolInvoker:
    """把单次 Graph Run 的执行作用域绑定到 Tool Executor 上。"""

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
    """确定性的首轮分类器，输出显式的安全 Routing Outcome。"""

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
    """Dense Retrieval -> Parent-Child Mapping -> Context Build -> Generation。"""

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
            raw_code = result.error.code if result.error else "retrieval_failed"
            message = result.error.message if result.error else "Retrieval failed."
            attempts = repair_count(state)
            if result.status == "failed" and raw_code in TRANSIENT_RETRIEVAL_CODES:
                # 瞬时的 provider/store 失败可进入修复；而诸如索引版本未激活这类
                # 拒绝是终态。
                return {
                    "error": failure(
                        "retrieval_unavailable",
                        message,
                        attempts=attempts,
                        details={"reason": raw_code},
                    )
                }
            return {"error": failure(raw_code, message, attempts=attempts)}
        data = result.data or {}
        results = list(data.get("results") or [])[: self.settings.max_retrieval_top_k]
        return {
            "error": None,
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
            # 没有已授权证据：绝不使用预训练知识填补企业事实。
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
        except Exception:  # noqa: BLE001 - provider 失败会转为可修复的错误
            return {
                "error": failure(
                    "llm_unavailable",
                    "The configured LLM could not complete the request safely.",
                    attempts=repair_count(state),
                )
            }
        return {
            "error": None,
            "answer_candidate": answer,
            "messages": [{"role": "assistant", "content": answer}],
        }


def general_chat(state: GraphState, llm: GeneralLLM, settings: Settings) -> dict[str, object]:
    prompt = (
        "You are a general internal assistant. Answer the user's general question directly. "
        "You cannot access enterprise documents or business data on this path.\n\n"
        f"Question: {state['user_input']}"
    )
    try:
        answer = llm.generate(prompt[: settings.llm_max_input_chars], state)
    except Exception:  # noqa: BLE001 - provider 失败会转为可修复的错误
        return {
            "error": failure(
                "llm_unavailable",
                "The configured LLM could not complete the request safely.",
                attempts=repair_count(state),
            )
        }
    return {
        "error": None,
        "answer_candidate": answer,
        "messages": [{"role": "assistant", "content": answer}],
    }


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
        error = dict(decision.error)
        error.setdefault("recoverable", str(error.get("code")) in RECOVERABLE_FAILURE_CODES)
        error.setdefault("repair_attempts", repair_count(state))
        error.setdefault("details", {})
        return {"tool_calls": [], "error": error}
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
        "error": None,
        "tool_calls": [],
        "answer_candidate": answer,
        "messages": [{"role": "assistant", "content": answer}],
    }


def tools_condition(state: GraphState, budgets: ExecutionBudgets | None = None) -> str:
    """仅为条件边；它不执行 Tool，也不修改 State。"""

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
    """把已生成的规范化 Tool Result 映射到声明的 State 字段。"""

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
    reasons = [str(reason) for reason in (validation.get("reasons") or [])]
    terminal_error = state.get("error") or {}
    safe_failure = _safe_failure(str(terminal_error.get("code") or ""))
    if safe_failure:
        # 终态（未修复）的失败结果陈述真实的、安全的原因，而不是用一条通用校验
        # 消息把它掩盖掉。
        answer = safe_failure
    elif status == "insufficient_evidence":
        answer = (
            "Insufficient evidence is available in the authorized knowledge sources "
            "to answer this question."
        )
    elif status == "invalid_result":
        answer = _safe_invalid_result(reasons) or (
            "The answer could not be validated against the available evidence."
        )
    elif status == "validation_error":
        answer = "The answer could not be validated safely."
    return {"final_response": answer[:20000]}


def _safe_failure(code: str) -> str | None:
    """针对终态（未修复）失败码的、对用户安全的结果措辞。"""

    if code in {"llm_unavailable", "unsupported_capability"}:
        return (
            "The configured language model is unavailable for this request. "
            "The request was stopped without producing an answer."
        )
    if code == "retrieval_unavailable":
        return (
            "Knowledge retrieval is temporarily unavailable, so the request was stopped "
            "instead of answering without authorized evidence."
        )
    if code == "index_version_not_active":
        return "The requested knowledge index version is not available for retrieval."
    return None


def _safe_invalid_result(reasons: list[str]) -> str | None:
    """被拒绝或失败的 Tool Result 得到显式且不误导的安全结果。"""

    for reason in reasons:
        if not reason.startswith("tool_error:"):
            continue
        code = reason.split(":", 1)[1]
        if code in {"sql_rejected", "sensitive_column_blocked", "invalid_columns"}:
            return (
                "The requested operation was rejected by the data security boundary. "
                "It was not executed."
            )
        if code in {"approval_rejected", "approval_expired", "approval_pending"}:
            return "The requested export was not approved, so it was not executed."
        if code in {"query_timeout"}:
            return "The query exceeded the configured time limit and was stopped."
        if code in {"database_unavailable", "database_error"}:
            return "The structured data source is temporarily unavailable for this request."
        return f"The structured data operation could not be completed: {code}."
    return None


def _route_main(state: GraphState) -> str:
    outcome = state.get("routing_outcome")
    if outcome != "routed":
        return "safe_response"
    return state.get("current_path") or "safe_response"


def _route_validation(state: GraphState, budgets: ExecutionBudgets) -> str:
    """至多一次有界 revision，否则以校验结果直接 finalize。

    答案修订预算按候选级计算：只允许对 ``retryable_validation_failure`` 修订，安全
    终态（如 insufficient_evidence / invalid_result）不会进入修订。
    """

    if should_revise(state, budgets):
        return "revise_knowledge" if state.get("intent") == "knowledge" else "revise_structured"
    return "response_finalize"


def repair_guard(state: GraphState) -> dict[str, object]:
    """在有界重试前推进 repair 预算并清空路径结果。

    只有 repair-eligible 的失败才会到达此节点，因此预算在每次修复尝试时恰好消耗
    一次，且绝不会因安全、权限或校验结果而消耗。计数器存放在 ``error.repair_attempts``
    中，使 State 只保留一个有界信封。
    """

    attempts = repair_count(state) + 1
    return {
        "error": {
            "code": str((state.get("error") or {}).get("code", "unknown")),
            "message": "Transient failure; retrying within the repair budget.",
            "recoverable": True,
            "repair_attempts": attempts,
            "details": dict((state.get("error") or {}).get("details") or {}),
        },
        "answer_candidate": None,
        "answer_validation": None,
        "retrieval_results": [],
        "context": None,
        "citations": [],
    }


def _route_assembly(state: GraphState, budgets: ExecutionBudgets) -> str:
    """在预算内修复瞬时路径失败，否则继续进入校验。"""

    if is_recoverable(state) and budgets.can_repair(repair_count(state)):
        return "repair"
    return "answer_validation"


def _route_repair(state: GraphState) -> str:
    """重新进入抛出该瞬时失败的声明阶段。"""

    intent = state.get("intent")
    if intent == "knowledge":
        return "retrieval"
    if intent == "structured_data":
        return "model_node"
    return "general_chat"


def _clear_repair_error(state: GraphState) -> dict[str, object]:
    """清除已解决的瞬时失败，同时保留已消耗的 repair 预算。"""

    attempts = repair_count(state)
    if not attempts:
        return {"error": None}
    return {
        "error": {
            "code": "",
            "message": "",
            "recoverable": False,
            "repair_attempts": attempts,
            "details": dict((state.get("error") or {}).get("details") or {}),
        }
    }


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
        # 开发默认：stub registry 使骨架在没有基础设施的情况下仍可测试。
        # 运行时始终注入真正的 scoped Tool Executor。
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
    graph.add_node("repair_guard", repair_guard)
    graph.add_node("repair_next", _clear_repair_error)

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
    graph.add_conditional_edges(
        "response_assembly",
        lambda state: _route_assembly(state, limits),
        {
            "repair": "repair_guard",
            "answer_validation": "answer_validation",
        },
    )
    graph.add_conditional_edges(
        "repair_guard",
        _route_repair,
        {
            "retrieval": "repair_next",
            "model_node": "repair_next",
            "general_chat": "repair_next",
        },
    )
    graph.add_conditional_edges(
        "repair_next",
        _route_repair,
        {
            "retrieval": "retrieval",
            "model_node": "model_node",
            "general_chat": "general_chat",
        },
    )
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
