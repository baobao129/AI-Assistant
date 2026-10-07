"""Bounded Graph repair path tests (GRAPH_CONTRACT Graph-005 / Graph-009).

`max_repair_attempts` must be a real, separate budget: transient failures may be repaired
within it, while security rejections, permission failures, and validation outcomes must
never consume it or be retried to bypass a boundary.
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
    """Fails with a transient retrieval error a fixed number of times, then succeeds."""

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
    global _KnowledgeInput
    from backend.tools.mock_tools import KnowledgeSearchInput

    _KnowledgeInput = KnowledgeSearchInput


_install_input_models()


class FlakyLLM:
    """Raises a provider failure a fixed number of times, then answers."""

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
    # One more failure than the budget allows: the Run must terminate safely.
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
    assert "temporarily unavailable" in result["final_response"]


def test_zero_repair_budget_does_not_retry():
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
    llm = FlakyLLM(failures=1, answer="Hello there, how can I help?")
    result = _run("say hello", llm=llm, budgets=ExecutionBudgets(max_repair_attempts=2))
    assert llm.calls == 2
    assert result["error"] is None
    assert "how can I help" in result["final_response"]


def test_general_path_llm_failure_beyond_budget_is_terminal_and_safe():
    llm = FlakyLLM(failures=99)
    result = _run("say hello", llm=llm, budgets=ExecutionBudgets(max_repair_attempts=1))
    assert llm.calls == 2
    assert result["error"]["code"] == "llm_unavailable"
    assert "language model is unavailable" in result["final_response"]


def test_tool_rejection_is_never_repaired():
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
    """The two budgets must not consume one another."""

    state = initial_state(
        conversation_id="c", thread_id="t", run_id="r", user_input="policy document"
    )
    state["error"] = failure("llm_unavailable", "x", attempts=2)
    state["answer_revision_count"] = 1
    assert repair_count(state) == 2
    assert state["answer_revision_count"] == 1


def test_scoped_tool_context_is_untouched_by_repair():
    """Repair re-enters the Tool boundary with the same authorization scope."""

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
