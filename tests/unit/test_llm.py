"""LLM provider 的能力声明与 CapabilityResolver 解析测试。"""

import pytest

from backend.domain.state import initial_state
from backend.infrastructure.config import Settings
from backend.infrastructure.llm import (
    CapabilityResolver,
    LLMCapabilities,
    LocalOpenAICompatibleLLM,
    MockGeneralLLM,
    RemoteOpenAICompatibleLLM,
    UnsupportedCapabilityError,
    create_llm_provider,
)


def test_remote_is_the_default_complete_capability_profile_without_startup_connection(monkeypatch):
    # 显式断言默认值，避免外部导出的开发 profile 改变本用例要验证的行为。
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = Settings(_env_file=None, llm_provider="remote")
    provider = create_llm_provider(settings)

    assert isinstance(provider, RemoteOpenAICompatibleLLM)
    assert provider.capabilities == LLMCapabilities(
        streaming=True, structured_output=True, tool_calling=True
    )
    assert "API key" in provider.generate(
        "hello", initial_state(conversation_id="c", thread_id="t", run_id="r", user_input="hello")
    )


def test_local_profile_declares_each_capability_independently():
    settings = Settings(
        _env_file=None,
        llm_provider="local",
        local_llm_streaming=True,
        local_llm_structured_output=False,
        local_llm_tool_calling=True,
    )
    provider = create_llm_provider(settings)

    assert isinstance(provider, LocalOpenAICompatibleLLM)
    assert provider.capabilities.streaming
    assert not provider.capabilities.structured_output
    assert provider.capabilities.tool_calling
    with pytest.raises(UnsupportedCapabilityError) as exc_info:
        CapabilityResolver.require(provider, "structured_output")
    assert exc_info.value.missing == ("structured_output",)


def test_provider_without_declared_capabilities_is_not_treated_as_supported():
    class UndeclaredProvider:
        def generate(self, user_input, state):
            return "answer"

    assert CapabilityResolver.for_provider(UndeclaredProvider()) == LLMCapabilities()


def test_mock_provider_declares_complete_capability_profile():
    assert CapabilityResolver.require(
        MockGeneralLLM(), "streaming", "structured_output", "tool_calling"
    ).tool_calling
