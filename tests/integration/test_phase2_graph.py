"""Phase 2 路由意图、能力边界与 General 路径不使用企业 Tool 的测试。"""

from backend.domain.state import initial_state
from backend.graph.main import IntentRouter, build_main_graph
from backend.infrastructure.config import Settings
from backend.tools.mock_tools import default_registry
from backend.tools.registry import ToolExecutor


def test_router_exposes_three_intents_and_all_safe_outcomes():
    router = IntentRouter()
    assert router.route("policy document")["intent"] == "knowledge"
    assert router.route("show sales amount")["intent"] == "structured_data"
    assert router.route("say hello")["intent"] == "general"
    for query, outcome in (
        ("", "invalid_request"),
        ("policy document sales amount", "unsupported_mixed_query"),
        ("__routing_error__", "routing_error"),
    ):
        decision = router.route(query)
        assert decision["routing_outcome"] == outcome
        assert decision["current_path"] is None


def test_local_without_tool_calling_is_rejected_at_structured_boundary():
    # local profile 未声明 tool_calling 能力，进入 structured_data 边界时应安全拒绝。
    graph = build_main_graph(
        executor=ToolExecutor(default_registry()),
        settings=Settings(_env_file=None, llm_provider="local"),
    )
    result = graph.invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="show sales")
    )

    assert result["tool_results"] == []
    assert result["error"]["code"] == "unsupported_capability"
    assert result["final_response"]


def test_general_path_never_invokes_enterprise_tools():
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def invoke(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("General path invoked a Tool")

    executor = RecordingExecutor()
    result = build_main_graph(executor=executor).invoke(
        initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="say hello")
    )

    # General 路径若调用任何 Tool 都会触发 RecordingExecutor 抛错，从而暴露越界。
    assert result["current_path"] == "general"
    assert executor.calls == []
