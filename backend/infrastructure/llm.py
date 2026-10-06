from typing import Protocol

from backend.domain.state import GraphState


class GeneralLLM(Protocol):
    def generate(self, user_input: str, state: GraphState) -> str: ...


class MockGeneralLLM:
    def generate(self, user_input: str, state: GraphState) -> str:
        return f"General response: {user_input.strip()}"
