"""Resume 的 Checkpoint 语义回归测试（Final Review MAJOR 4）。

interrupt -> approve -> successor resume 必须从 Checkpoint/可恢复 State 继续：

- 后继 Run 只传「Identity + approval 绑定」增量，messages、tool_results、
  answer_revision_count 与已完成的 Tool 执行事实都由 Checkpoint 恢复；
- 不重复执行已完成的 ``database_query``，只执行一次被批准的 ``data_export``，
  且使用 approval 绑定的同一个 ``query_result_id``；
- 独立预算（tool loop / repair / answer revision）不被重置。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from backend.domain.state import initial_state
from backend.graph.main import build_main_graph
from backend.infrastructure.config import Settings
from backend.tools.contracts import ToolError, ToolResult

THREAD_ID = "thread-1"
USER_INPUT = "export sales amount data to csv"
BOUND_QUERY_RESULT_ID = "query-result-1"
APPROVAL_ID = "approval-1"


class RecordingStructuredExecutor:
    """记录每次 Tool 调用的确定性 Structured Data 执行器。

    第一次 data_export 返回 ``approval_required``（提案暂停），之后的调用按已批准路径
    返回成功；database_query 每次都记录，供断言「Resume 不重复查询」。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.exported: list[str] = []
        self._export_proposals = 0

    def invoke(
        self,
        tool_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> ToolResult:
        self.calls.append((tool_id, tool_call_id))
        if tool_id == "database_query":
            return ToolResult(
                tool_id=tool_id,
                tool_call_id=tool_call_id,
                status="success",
                data={
                    "query_result_id": BOUND_QUERY_RESULT_ID,
                    "columns": ["amount"],
                    "rows": [[120.0]],
                    "row_count": 1,
                    "truncated": False,
                    "query_summary": {"tables": ["sales_orders"], "duration_ms": 1},
                },
                metadata={"sql_validation": "accepted"},
            )
        if tool_id == "data_export":
            self._export_proposals += 1
            if self._export_proposals == 1:
                return ToolResult(
                    tool_id=tool_id,
                    tool_call_id=tool_call_id,
                    status="rejected",
                    data={
                        "approval_lifecycle": "pending",
                        "approval_request_id": APPROVAL_ID,
                        "summary": {"row_count": 1, "columns": ["amount"], "format": "csv"},
                        "expires_at": "2030-01-01T00:00:00+00:00",
                    },
                    error=ToolError(code="approval_required", message="Approval is required."),
                )
            self.exported.append(str(arguments.get("query_result_id")))
            return ToolResult(
                tool_id=tool_id,
                tool_call_id=tool_call_id,
                status="success",
                data={
                    "status": "success",
                    "approval_lifecycle": "approved",
                    "approval_request_id": APPROVAL_ID,
                    "artifact_id": "artifact-1",
                    "row_count": 1,
                    "format": "csv",
                },
            )
        raise AssertionError(f"unexpected tool: {tool_id}")


def _graph(executor: RecordingStructuredExecutor):
    return build_main_graph(
        executor=executor,
        checkpointer=InMemorySaver(),
        settings=Settings(_env_file=None, llm_provider="mock", sql_agent_provider="scripted"),
    )


def _tool_calls(executor: RecordingStructuredExecutor, tool_id: str) -> int:
    return sum(1 for call in executor.calls if call[0] == tool_id)


def test_resume_preserves_checkpoint_state_budgets_and_execution_facts():
    executor = RecordingStructuredExecutor()
    graph = _graph(executor)
    config = {"configurable": {"thread_id": THREAD_ID}}

    # 第一段：Run 抛出 export 提案并停在 awaiting_approval（interrupt）。
    # 预置一个非零 revision 计数，使「Resume 不得重置计数器」的断言非平凡。
    first = initial_state(
        conversation_id="conv-1", thread_id=THREAD_ID, run_id="run-1", user_input=USER_INPUT
    )
    first["answer_revision_count"] = 1
    paused = graph.invoke(first, config)

    assert paused["interrupt_status"] == "pending"
    assert paused["approval_request_id"] == APPROVAL_ID
    assert paused["query_result_id"] == BOUND_QUERY_RESULT_ID
    assert _tool_calls(executor, "database_query") == 1
    assert executor.exported == []
    paused_revisions = int(paused["answer_revision_count"] or 0)
    assert paused_revisions >= 1
    paused_results = list(paused["tool_results"])

    # 第二段：批准后的 successor Run 只传 Identity + approval 绑定增量
    # （与 RunService.resume 完全一致），其余状态由 Checkpoint 恢复。
    successor_input: dict[str, Any] = {
        "conversation_id": "conv-1",
        "thread_id": THREAD_ID,
        "run_id": "run-2",
        "user_input": USER_INPUT,
        "approval_request_id": APPROVAL_ID,
        "interrupt_status": "approved",
        "query_result_id": BOUND_QUERY_RESULT_ID,
    }
    resumed = graph.invoke(successor_input, config)

    # 不重复已完成的查询；导出的 snapshot 就是 approval 绑定的那一个。
    assert _tool_calls(executor, "database_query") == 1
    assert executor.exported == [BOUND_QUERY_RESULT_ID]

    # 执行历史累加而不是被重置，且保留 approval / 绑定 / 计数器。
    assert list(resumed["tool_results"])[: len(paused_results)] == paused_results
    assert len(resumed["tool_results"]) > len(paused_results)
    assert resumed["approval_request_id"] == APPROVAL_ID
    assert resumed["query_result_id"] == BOUND_QUERY_RESULT_ID
    assert resumed["interrupt_status"] == "approved"
    assert int(resumed["answer_revision_count"] or 0) == paused_revisions


def test_request_initialize_preserves_cross_run_counters():
    # request_initialize 只清空请求级结果字段，绝不重置跨 Run 的预算计数器。
    from backend.graph.main import request_initialize

    state = initial_state(
        conversation_id="conv-1", thread_id=THREAD_ID, run_id="run-1", user_input=USER_INPUT
    )
    state["answer_revision_count"] = 2
    state["error"] = {
        "code": "",
        "message": "",
        "recoverable": False,
        "repair_attempts": 3,
        "details": {},
    }

    update = request_initialize(state)

    assert update["answer_revision_count"] == 2
    assert update["error"] == state["error"]
    assert update["answer_candidate"] is None
    assert update["final_response"] is None
