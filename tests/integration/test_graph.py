"""主图端到端路由、Tool Loop 有界性与 Checkpoint 调用的进程内集成测试。"""

from pathlib import Path

from backend.domain.checkpoint import checkpoint_context
from backend.domain.state import initial_state
from backend.graph.main import build_main_graph
from backend.graph.policies import ExecutionBudgets
from backend.tools.mock_tools import default_registry
from backend.tools.registry import ToolExecutor


def run(query: str):
    # 用默认 Registry 构造主图并直接 invoke，不接数据库与外部 provider。
    graph = build_main_graph(ToolExecutor(default_registry()))
    return graph.invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input=query)
    )


def test_three_intents_route_and_terminate():
    assert run("What does the policy document say?")["current_path"] == "knowledge"
    assert run("Show sales amount")["current_path"] == "structured_data"
    assert run("Say hello")["current_path"] == "general"
    assert all(
        run(query)["final_response"] for query in ("policy document", "sales amount", "hello")
    )


def test_non_routed_outcomes_keep_current_path_null():
    # 非 routed 的 Routing Outcome 一律保持 current_path 为 None。
    for query, outcome in [
        ("", "invalid_request"),
        ("policy document sales amount", "unsupported_mixed_query"),
        ("__routing_error__", "routing_error"),
    ]:
        result = run(query)
        assert result["routing_outcome"] == outcome
        assert result["current_path"] is None
        assert result["final_response"]


def test_sql_loop_uses_registry_and_is_bounded():
    result = run("show sales amount")
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["tool_id"] == "database_query"
    assert len(result["tool_results"]) <= 8


def test_unsafe_sql_is_rejected_without_database_side_effect():
    result = run("drop sales table")
    assert result["sql_validation"]["status"] == "rejected"
    assert result["tool_results"][0]["status"] == "rejected"


def test_graph_accepts_explicit_budgets():
    graph = build_main_graph(budgets=ExecutionBudgets())
    result = graph.invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="sales")
    )
    assert result["final_response"]


def test_sqlite_checkpoint_graph_invocation(tmp_path: Path):
    # 传入 checkpointer 并显式提供 thread_id，验证图可在持久化检查点上执行。
    with checkpoint_context("sqlite", tmp_path / "checkpoints.sqlite") as saver:
        graph = build_main_graph(checkpointer=saver)
        result = graph.invoke(
            initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="hello"),
            {"configurable": {"thread_id": "t"}},
        )
    assert "General response" in result["final_response"]
    assert "hello" in result["final_response"]
