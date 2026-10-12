"""Graph 有界修复路径测试（GRAPH_CONTRACT Graph-005 / Graph-009）。

``max_repair_attempts`` 必须是一个真实且独立的预算：瞬时故障可在其范围内修复，
而安全拒绝、权限失败与校验结果绝不消耗该预算，也不会被重试以绕过边界。
"""

from __future__ import annotations

from backend.domain.state import initial_state
from backend.graph.main import (
    RECOVERABLE_FAILURE_CODES,
    build_main_graph,
    failure,
    is_recoverable,
    repair_count,
)
from backend.graph.policies import ExecutionBudgets
from backend.infrastructure.config import Settings
from backend.tools.contracts import ToolError, ToolExecutionContext, ToolResult
from backend.tools.registry import ToolExecutor, ToolRegistry


class FlakyKnowledgeTool:
    """先以瞬时检索错误失败固定次数，之后转为成功。"""

    def __init__(self, failures: int) -> None:
        from backend.tools.contracts import ToolMetadata

        class Input:
            def __init__(self) -> None:

                self.model_dump = lambda: {}

        self.metadata = ToolMetadata("knowledge_search", "1", "low", "test")
        self.input_model = _KnowledgeInput
        self.failures = failures
        self.calls = 0

    def execute(self, tool_call_id, arguments, context=None):
        self.calls += 1
        if self.calls <= self.failures:
            return ToolResult(
                tool_id="knowledge_search",
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="vector_store_unavailable", message="temporarily unavailable"),
            )
        return ToolResult(
            tool_id="knowledge_search",
            tool_call_id=tool_call_id,
            status="success",
            data={
                "query": arguments.query,
                "truncated": False,
                "results": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "parent_chunk_id": "p1",
                        "index_version": "v1",
                        "content": "The travel policy requires economy class for short flights.",
                        "score": 0.9,
                        "metadata": {"label": "Travel Policy", "location": "chunk-0"},
                        "citation": {
                            "source_id": "d1",
                            "location": "chunk-0",
                            "label": "Travel Policy",
                        },
                    }
                ],
            },
        )


def _install_input_models() -> None:
    # 延迟导入 Tool 输入模型，避免在模块顶层引入对 backend 的过早依赖。
    global _KnowledgeInput
    from backend.tools.mock_tools import KnowledgeSearchInput

    _KnowledgeInput = KnowledgeSearchInput


_install_input_models()


class FlakyLLM:
    """先抛 provider 失败固定次数，之后返回答案。"""

    def __init__(self, failures: int, answer: str = "Grounded answer.") -> None:
        self.failures = failures
        self.calls = 0
        self.answer = answer

    def generate(self, user_input: str, state) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("provider unavailable")
        return self.answer


def _run(query: str, *, tool: FlakyKnowledgeTool | None = None, llm=None, budgets=None):
    # 组装最小执行环境：可选注入易失败 Tool/LLM 与预算，再 invoke 主图。
    registry = ToolRegistry([])
    if tool is not None:
        registry.register(tool)
    graph = build_main_graph(
        executor=ToolExecutor(registry),
        settings=Settings(_env_file=None, llm_provider="mock"),
        general_llm=llm,
        budgets=budgets or ExecutionBudgets(),
    )
    return graph.invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input=query)
    )


def test_transient_retrieval_failure_is_repaired_within_budget():
    # 一次瞬时检索失败应在预算内被修复，最终 Run 不残留 error。
    tool = FlakyKnowledgeTool(failures=1)
    result = _run(
        "policy document question",
        tool=tool,
        llm=FlakyLLM(failures=0, answer="Travel Policy requires economy class for flights."),
    )
    assert tool.calls == 2, "the declared stage must be re-entered exactly once"
    assert result["answer_candidate"]
    assert result["error"] is None
    assert result["final_response"]


def test_repair_budget_is_respected_and_failure_becomes_terminal():
    # 失败次数比预算允许的更多：Run 必须安全终止，而不是无限重试。
    tool = FlakyKnowledgeTool(failures=99)
    result = _run(
        "policy document question",
        tool=tool,
        llm=FlakyLLM(failures=0),
        budgets=ExecutionBudgets(max_repair_attempts=1),
    )
    assert tool.calls == 2, "one initial attempt plus one repair attempt"
    assert result["error"]["code"] == "retrieval_unavailable"
    assert result["error"]["repair_attempts"] == 1
    assert "暂时不可用" in result["final_response"]


def test_zero_repair_budget_does_not_retry():
    # 预算为 0 时不允许任何重试，Tool 只被调用一次。
    tool = FlakyKnowledgeTool(failures=1)
    result = _run(
        "policy document question",
        tool=tool,
        llm=FlakyLLM(failures=0),
        budgets=ExecutionBudgets(max_repair_attempts=0),
    )
    assert tool.calls == 1
    assert result["error"]["code"] == "retrieval_unavailable"


def test_llm_provider_failure_on_general_path_is_repaired():
    # General 路径上的 provider 瞬时失败同样在预算内被修复。
    llm = FlakyLLM(failures=1, answer="Hello there, how can I help?")
    result = _run("say hello", llm=llm, budgets=ExecutionBudgets(max_repair_attempts=2))
    assert llm.calls == 2
    assert result["error"] is None
    assert "how can I help" in result["final_response"]


def test_general_path_llm_failure_beyond_budget_is_terminal_and_safe():
    # 超出预算的 provider 失败必须落到安全的终态回答，且错误码为 llm_unavailable。
    llm = FlakyLLM(failures=99)
    result = _run("say hello", llm=llm, budgets=ExecutionBudgets(max_repair_attempts=1))
    assert llm.calls == 2
    assert result["error"]["code"] == "llm_unavailable"
    assert "语言模型暂时不可用" in result["final_response"]


def test_incompatible_index_never_repairs_or_generates():
    class Incompatible(FlakyKnowledgeTool):
        def execute(self, tool_call_id, arguments, context=None):
            self.calls += 1
            return ToolResult(
                tool_id="knowledge_search",
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="knowledge_index_incompatible", message="unavailable"),
            )

    tool = Incompatible(failures=0)
    llm = FlakyLLM(failures=0)
    result = _run("policy document question", tool=tool, llm=llm)
    assert tool.calls == 1 and llm.calls == 0
    assert repair_count(result) == 0
    assert result["error"]["code"] == "knowledge_index_incompatible"
    assert "不兼容" in result["final_response"]


def test_tool_rejection_is_never_repaired():
    # reject 属于安全边界拒绝，绝不能被当作瞬时故障重试。
    class RejectingTool:
        def __init__(self) -> None:
            from backend.tools.contracts import ToolMetadata

            self.metadata = ToolMetadata("knowledge_search", "1", "low", "test")
            self.input_model = _KnowledgeInput
            self.calls = 0

        def execute(self, tool_call_id, arguments, context=None):
            self.calls += 1
            return ToolResult(
                tool_id="knowledge_search",
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(
                    code="index_version_not_active", message="That index version is not active."
                ),
            )

    tool = RejectingTool()
    result = _run(
        "policy document question",
        tool=tool,
        llm=FlakyLLM(failures=0),
        budgets=ExecutionBudgets(max_repair_attempts=3),
    )
    assert tool.calls == 1, "a rejection must not be retried"
    assert result["final_response"]


def test_repair_classification_and_counter_helpers():
    # 只有可恢复故障码才可修复；安全类错误码必须被排除在 RECOVERABLE_FAILURE_CODES 之外。
    assert "llm_unavailable" in RECOVERABLE_FAILURE_CODES
    assert "sql_rejected" not in RECOVERABLE_FAILURE_CODES
    assert "approval_rejected" not in RECOVERABLE_FAILURE_CODES
    assert "query_timeout" not in RECOVERABLE_FAILURE_CODES
    assert is_recoverable({"error": failure("llm_unavailable", "x")}) is True
    assert is_recoverable({"error": failure("sql_rejected", "x")}) is False
    assert is_recoverable({"error": None}) is False
    assert repair_count({"error": failure("llm_unavailable", "x", attempts=2)}) == 2
    assert repair_count({"error": None}) == 0
    assert repair_count({"error": {"repair_attempts": "bad"}}) == 0


def test_repair_count_is_separate_from_revision_count():
    """修复预算与修订预算不得相互消耗。"""

    state = initial_state(
        conversation_id="c", thread_id="t", run_id="r", user_input="policy document"
    )
    state["error"] = failure("llm_unavailable", "x", attempts=2)
    state["answer_revision_count"] = 1
    assert repair_count(state) == 2
    assert state["answer_revision_count"] == 1


def test_scoped_tool_context_is_untouched_by_repair():
    """修复会带着相同的授权范围重新进入 Tool 边界。"""

    seen: list[ToolExecutionContext | None] = []

    class RecordingFailure:
        def __init__(self) -> None:
            from backend.tools.contracts import ToolMetadata

            self.metadata = ToolMetadata("knowledge_search", "1", "low", "test")
            self.input_model = _KnowledgeInput

        def execute(self, tool_call_id, arguments, context=None):
            seen.append(context)
            return ToolResult(
                tool_id="knowledge_search",
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="vector_store_unavailable", message="down"),
            )

    registry = ToolRegistry([RecordingFailure()])
    graph = build_main_graph(
        executor=ToolExecutor(registry),
        settings=Settings(_env_file=None, llm_provider="mock"),
        general_llm=FlakyLLM(failures=0),
        budgets=ExecutionBudgets(max_repair_attempts=1),
        context=ToolExecutionContext(user_id="u1", authorized_document_ids=("d1",)),
    )
    graph.invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="policy document")
    )
    assert len(seen) == 2
    assert all(context is not None and context.user_id == "u1" for context in seen)
    assert all(context.authorized_document_ids == ("d1",) for context in seen)
