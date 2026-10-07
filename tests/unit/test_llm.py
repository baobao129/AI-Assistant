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
    # 显式提供空密钥与默认地址：任何依赖（例如 pymilvus 导入时会把 .env 注入进程环境）
    # 或外部导出的变量都不得让本用例触发真实网络请求。
    for key in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        _env_file=None,
        llm_provider="remote",
        llm_api_key="",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
    )
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
