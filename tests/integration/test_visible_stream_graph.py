"""Real LangGraph custom stream separates visible content and authoritative validated final."""

from backend.domain.state import initial_state
from backend.graph.main import build_main_graph
from backend.infrastructure.config import Settings


def test_custom_stream_is_incremental_before_final_without_hidden_state():
    class Provider:
        def generate_stream(self, prompt, state):
            yield "你好，"
            yield "可以开始了。"

        def generate(self, prompt, state):
            raise AssertionError("Visible generation must stream")

    graph = build_main_graph(
        settings=Settings(_env_file=None, llm_provider="mock"), general_llm=Provider()
    )
    packets = list(
        graph.stream(
            initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="你好"),
            stream_mode=["values", "custom"],
        )
    )
    custom = [payload for mode, payload in packets if mode == "custom"]
    assert custom[0] == {"type": "answer_delta", "delta": "", "reset": True}
    assert "".join(packet["delta"] for packet in custom) == "你好，可以开始了。"
    final = [payload for mode, payload in packets if mode == "values"][-1]
    assert final["answer_validation"]["status"] == "validated"
    assert final["final_response"] == "你好，可以开始了。"
