"""Tool Registry 的契约工具列表与 ToolExecutor 归一化结果测试。"""

from backend.tools.mock_tools import default_registry
from backend.tools.registry import ToolExecutor


def test_registry_has_contract_tools_and_executor_returns_normalized_result():
    # Registry 暴露的 Tool 集合必须与契约一致，且执行结果归一化为 rejected。
    registry = default_registry()
    assert registry.list_tool_ids() == (
        "data_export",
        "database_query",
        "database_schema",
        "knowledge_search",
    )
    result = ToolExecutor(registry).invoke("database_query", "call-1", {"sql": "DROP TABLE x"})
    assert result.status == "rejected"
    assert result.error and result.error.code == "sql_rejected"


def test_tool_executor_does_not_receive_state():
    # Tool 只接收显式入参，不得触及 Graph State（如 messages）。
    executor = ToolExecutor(default_registry())
    result = executor.invoke("knowledge_search", "call-2", {"query": "policy"})
    assert "messages" not in result.data
    assert result.status == "success"
