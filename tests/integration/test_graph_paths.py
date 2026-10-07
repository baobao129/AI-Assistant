"""Graph 路径、Tool Registry 与 Answer Validation 边界测试。

这些用例基于进程内桩运行，无需数据库、Milvus 或 provider 凭据即可执行；
对应的实时验证见 ``scripts/verify_live.py``。
"""

from __future__ import annotations

from backend.domain.state import initial_state
from backend.graph.main import IntentRouter, build_main_graph
from backend.graph.policies import ExecutionBudgets
from backend.graph.validation import should_revise, validate_answer
from backend.infrastructure.config import Settings
from backend.tools.contracts import ToolError, ToolExecutionContext, ToolResult
from backend.tools.registry import ToolExecutor, ToolRegistry


class RecordingTool:
    """最小可注册 Tool，用于观察 Registry 与 Executor 的行为。"""

    def __init__(self, tool_id: str, result: ToolResult | None = None) -> None:
        from pydantic import BaseModel, ConfigDict

        from backend.tools.contracts import ToolMetadata

        class Input(BaseModel):
            model_config = ConfigDict(extra="forbid")
            question: str = ""

        self.metadata = ToolMetadata(tool_id, "1", "low", "test")
        self.input_model = Input
        self.calls: list[tuple[str, dict, ToolExecutionContext | None]] = []
        self._result = result

    def execute(self, tool_call_id, arguments, context=None):
        self.calls.append((tool_call_id, arguments.model_dump(), context))
        if self._result is not None:
            return self._result
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={"question": arguments.question},
        )


def test_executor_rejects_unknown_and_invalid_tool_input():
    executor = ToolExecutor(ToolRegistry([RecordingTool("echo")]))
    unknown = executor.invoke("missing", "call-1", {})
    assert unknown.status == "rejected"
    assert unknown.error and unknown.error.code == "unknown_tool"
    invalid = executor.invoke("echo", "call-2", {"unexpected": 1})
    assert invalid.status == "rejected"
    assert invalid.error and invalid.error.code == "validation_error"


def test_executor_passes_explicit_execution_context_only():
    tool = RecordingTool("knowledge_search")
    executor = ToolExecutor(ToolRegistry([tool]))
    context = ToolExecutionContext(user_id="u1", run_id="r1", authorized_document_ids=("d1",))
    executor.invoke("knowledge_search", "call-1", {"question": "policy"}, context)
    assert tool.calls[0][2] == context
    # Graph State 绝不会传给 Tool。
    assert "messages" not in tool.calls[0][1]


def test_router_outcomes_keep_current_path_null():
    router = IntentRouter()
    for query, outcome in (
        ("", "invalid_request"),
        ("policy document sales amount", "unsupported_mixed_query"),
        ("__routing_error__", "routing_error"),
    ):
        decision = router.route(query)
        assert decision["routing_outcome"] == outcome
        assert decision["current_path"] is None
        assert decision["intent"] is None


def test_general_path_never_calls_enterprise_tools():
    class RecordingExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def invoke(self, tool_id, tool_call_id, arguments, context=None):
            self.calls.append((tool_id, arguments))
            raise AssertionError("The General path invoked an enterprise Tool")

    executor = RecordingExecutor()
    result = build_main_graph(
        executor=executor, settings=Settings(_env_file=None, llm_provider="mock")
    ).invoke(initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="say hello"))
    assert result["current_path"] == "general"
    assert executor.calls == []


def test_knowledge_path_without_evidence_returns_safe_outcome():
    class EmptyKnowledge(RecordingTool):
        def execute(self, tool_call_id, arguments, context=None):
            return ToolResult(
                tool_id="knowledge_search",
                tool_call_id=tool_call_id,
                status="success",
                data={"query": arguments.question, "results": [], "truncated": False},
            )

    executor = ToolExecutor(ToolRegistry([EmptyKnowledge("knowledge_search")]))
    result = build_main_graph(
        executor=executor, settings=Settings(_env_file=None, llm_provider="mock")
    ).invoke(
        initial_state(
            conversation_id="c", thread_id="t", run_id="r", user_input="policy document question"
        )
    )
    assert result["answer_validation"]["status"] == "insufficient_evidence"
    assert "Insufficient evidence" in result["final_response"]


def test_structured_data_tool_failure_reaches_safe_terminal_outcome():
    class QueryInput(RecordingTool):
        """使用 Tool Contract 定义的输入模型，使 Executor 能真正走到 Tool。"""

        def __init__(self) -> None:
            super().__init__("database_query")
            from backend.tools.mock_tools import DatabaseQueryInput

            self.input_model = DatabaseQueryInput

        def execute(self, tool_call_id, arguments, context=None):
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="database_error", message="unavailable"),
            )

    executor = ToolExecutor(ToolRegistry([QueryInput()]))
    result = build_main_graph(
        executor=executor, settings=Settings(_env_file=None, llm_provider="mock")
    ).invoke(
        initial_state(
            conversation_id="c", thread_id="t", run_id="r", user_input="show sales amount"
        )
    )
    assert result["tool_results"][0]["status"] == "failed"
    assert result["answer_validation"]["status"] == "invalid_result"
    assert result["final_response"]


def test_tool_loop_is_bounded_by_max_tool_iterations():
    tool = RecordingTool("database_query")
    executor = ToolExecutor(ToolRegistry([tool]))
    result = build_main_graph(
        executor=executor,
        settings=Settings(_env_file=None, llm_provider="mock"),
        budgets=ExecutionBudgets(max_tool_iterations=1),
    ).invoke(
        initial_state(
            conversation_id="c", thread_id="t", run_id="r", user_input="show sales amount"
        )
    )
    assert len(result["tool_results"]) <= 1
    assert result["final_response"]


def test_tool_results_and_messages_accumulate_across_the_loop():
    """真实模型会连续调用多个工具；State 必须按契约累加结果与消息历史。"""

    # 使用真实 Tool 输入模型：与契约字段（含 source 等默认值）一致，
    # 避免桩输入模型因缺省字段丢掉校验路径。
    from backend.tools.data_tools import DatabaseQueryInput, DataExportInput

    class QueryTool(RecordingTool):
        """返回带 query_result_id 的成功结果，驱动脚本化模型继续提议导出。"""

        def __init__(self) -> None:
            super().__init__("database_query")
            self.input_model = DatabaseQueryInput

        def execute(self, tool_call_id, arguments, context=None):
            return ToolResult(
                tool_id="database_query",
                tool_call_id=tool_call_id,
                status="success",
                data={
                    "query_result_id": "qr-1",
                    "columns": ["amount"],
                    "rows": [[1]],
                    "row_count": 5,
                    "truncated": False,
                    "query_summary": {"tables": ["sales_orders"], "duration_ms": 1},
                },
            )

    class ExportTool(RecordingTool):
        """返回 approval_required，模拟 HITL 提案被拒（等待人工审批）。"""

        def __init__(self) -> None:
            super().__init__("data_export")
            self.input_model = DataExportInput

        def execute(self, tool_call_id, arguments, context=None):
            return ToolResult(
                tool_id="data_export",
                tool_call_id=tool_call_id,
                status="rejected",
                data={"approval_lifecycle": "pending", "approval_request_id": "ap-1"},
                error=ToolError(
                    code="approval_required", message="Approval is required before export."
                ),
            )

    executor = ToolExecutor(ToolRegistry([QueryTool(), ExportTool()]))
    result = build_main_graph(
        executor=executor, settings=Settings(_env_file=None, llm_provider="mock")
    ).invoke(
        initial_state(
            conversation_id="c",
            thread_id="t",
            run_id="r",
            user_input="export sales amount data to csv",
        )
    )

    # 两次工具调用 → 两条 Tool Result（修复前会被「替换」为 1 条，导致循环计数与事件缺失）。
    assert [item["tool_id"] for item in result["tool_results"]] == [
        "database_query",
        "data_export",
    ]
    assert [item["status"] for item in result["tool_results"]] == ["success", "rejected"]
    # 消息历史按执行顺序保留：模型建议 → 工具消息 → 模型建议 → 工具消息 → 最终回答；
    # 之后最多再加一次有界答案修订（max_answer_revisions = 1）。
    roles = [message.get("role") for message in result["messages"]]
    assert roles[:5] == ["assistant", "tool", "assistant", "tool", "assistant"]
    assert len(roles) <= 6
    assert result["approval_request_id"] == "ap-1"
    assert result["final_response"]


def test_answer_validation_revision_is_bounded():
    # 答案与检索证据无重叠时判为可重试失败，修订次数达到上限后不应再修订。
    state = initial_state(
        conversation_id="c", thread_id="t", run_id="r", user_input="policy document"
    )
    state["intent"] = "knowledge"
    state["answer_candidate"] = "An unrelated answer without any overlap."
    state["retrieval_results"] = [
        {
            "content": "The procurement policy requires three quotes.",
            "metadata": {"label": "Policy"},
        }
    ]
    state["context"] = "[Policy] The procurement policy requires three quotes."
    state["citations"] = [{"source_id": "d1", "label": "Policy", "location": "p1"}]
    first = validate_answer(state, ExecutionBudgets())
    assert first["answer_validation"]["status"] == "retryable_validation_failure"
    state["answer_validation"] = first["answer_validation"]
    assert should_revise(state, ExecutionBudgets(max_answer_revisions=1)) is True
    state["answer_revision_count"] = 1
    assert should_revise(state, ExecutionBudgets(max_answer_revisions=1)) is False


def test_safe_routing_outcomes_bypass_ordinary_validation():
    state = initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="mixed")
    state["routing_outcome"] = "unsupported_mixed_query"
    state["answer_candidate"] = "Unsupported mixed request."
    outcome = validate_answer(state, ExecutionBudgets())
    assert outcome["answer_validation"]["status"] == "validated"


def test_structured_validation_requires_truncation_disclosure():
    state = initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="show sales")
    state["intent"] = "structured_data"
    state["answer_candidate"] = "There are 5 matching rows."
    state["tool_results"] = [
        {
            "tool_id": "database_query",
            "status": "success",
            "data": {"row_count": 5, "truncated": True, "columns": ["amount"]},
        }
    ]
    outcome = validate_answer(state, ExecutionBudgets())
    assert "truncation_not_disclosed" in outcome["answer_validation"]["reasons"]
    state["answer_candidate"] = "There are 5 matching rows and the result was truncated."
    assert validate_answer(state, ExecutionBudgets())["answer_validation"]["status"] == "validated"
