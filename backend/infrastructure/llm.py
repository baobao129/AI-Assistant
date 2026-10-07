from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from backend.domain.state import GraphState

if TYPE_CHECKING:
    from backend.infrastructure.config import Settings


CapabilityName = Literal["streaming", "structured_output", "tool_calling"]


@dataclass(frozen=True)
class LLMCapabilities:
    """Provider metadata owned by the application, independent of provider name."""

    streaming: bool = False
    structured_output: bool = False
    tool_calling: bool = False

    def supports(self, capability: CapabilityName) -> bool:
        return bool(getattr(self, capability))

    def missing(self, required: tuple[CapabilityName, ...]) -> tuple[CapabilityName, ...]:
        return tuple(capability for capability in required if not self.supports(capability))


# The terminology contract calls this an LLM Capability. This alias keeps the
# metadata name discoverable without introducing a second representation.
CapabilityMetadata = LLMCapabilities


class UnsupportedCapabilityError(ValueError):
    def __init__(self, provider_name: str, missing: tuple[CapabilityName, ...]) -> None:
        self.provider_name = provider_name
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(f"Provider {provider_name!r} does not support: {names}")


class GeneralLLM(Protocol):
    def generate(self, user_input: str, state: GraphState) -> str: ...


class LLMProvider(GeneralLLM, Protocol):
    provider_name: str
    model: str
    capabilities: LLMCapabilities


class CapabilityResolver:
    """Resolves and checks project-owned capability metadata at workflow boundaries."""

    @staticmethod
    def for_provider(provider: object) -> LLMCapabilities:
        capabilities = getattr(provider, "capabilities", None)
        return capabilities if isinstance(capabilities, LLMCapabilities) else LLMCapabilities()

    @classmethod
    def require(cls, provider: object, *required: CapabilityName) -> LLMCapabilities:
        capabilities = cls.for_provider(provider)
        missing = capabilities.missing(tuple(required))
        if missing:
            provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__))
            raise UnsupportedCapabilityError(provider_name, missing)
        return capabilities


def provider_capabilities(provider: object) -> LLMCapabilities:
    """Compatibility helper for Graph/Application code at an adapter boundary."""

    return CapabilityResolver.for_provider(provider)


class MockGeneralLLM:
    provider_name = "mock"
    model = "mock-general"
    # The mock stands in for the complete-capability remote profile in tests.
    capabilities = LLMCapabilities(streaming=True, structured_output=True, tool_calling=True)

    def generate(self, user_input: str, state: GraphState) -> str:
        return f"General response: {user_input.strip()}"


class RemoteOpenAICompatibleLLM:
    """Small OpenAI-compatible adapter using the already-approved HTTPX dependency."""

    provider_name = "remote"
    capabilities = LLMCapabilities(streaming=True, structured_output=True, tool_calling=True)

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(self, user_input: str, state: GraphState) -> str:
        if not self.api_key:
            return "The configured remote LLM is unavailable until an API key is configured."
        return self._generate_request(user_input, require_api_key=True)

    def _generate_request(self, user_input: str, *, require_api_key: bool) -> str:
        if require_api_key and not self.api_key:
            return "The configured LLM is unavailable until credentials are configured."
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": user_input.strip()}],
            "stream": False,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, str) and content.strip():
                return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            pass
        return "The configured LLM could not complete the request safely."


class LocalOpenAICompatibleLLM(RemoteOpenAICompatibleLLM):
    provider_name = "local"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        capabilities: LLMCapabilities,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        self.capabilities = capabilities

    def generate(self, user_input: str, state: GraphState) -> str:
        # Local OpenAI-compatible servers commonly run without authentication.
        return self._generate_request(user_input, require_api_key=False)


class ExtractiveGroundedLLM:
    """Offline development/verification reader that can only quote supplied context.

    It is explicitly opt-in (``LLM_PROVIDER=extractive``) and used for local Demo and
    acceptance runs where no provider credential is available. Because it extracts
    sentences from the prompt's authorized context, it can never introduce enterprise
    facts from pretrained knowledge. It does not support tool calling.
    """

    provider_name = "extractive"
    model = "extractive-grounded"
    capabilities = LLMCapabilities(streaming=False, structured_output=False, tool_calling=False)

    CONTEXT_MARKER = "Context:"
    QUESTION_MARKER = "Question:"

    def generate(self, user_input: str, state: GraphState) -> str:
        del state
        context, question = self._split(user_input)
        if not context:
            return "insufficient evidence"
        question_terms = _terms(question)
        sentences = _sentences(context)
        scored = sorted(
            (
                (len(question_terms & _terms(sentence)), sentence)
                for sentence in sentences
                if sentence.strip()
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = [sentence for score, sentence in scored[:3] if score > 0]
        if not selected:
            # No sentence in the authorized context relates to the question.
            return "insufficient evidence"
        return " ".join(selected)[:2000]

    @classmethod
    def _split(cls, prompt: str) -> tuple[str, str]:
        if cls.CONTEXT_MARKER not in prompt:
            return "", prompt
        after_context = prompt.split(cls.CONTEXT_MARKER, 1)[1]
        if cls.QUESTION_MARKER in after_context:
            context, question = after_context.split(cls.QUESTION_MARKER, 1)
        else:
            context, question = after_context, ""
        return context.strip(), question.strip()


_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "does",
        "what",
        "when",
        "where",
        "which",
        "about",
        "into",
        "than",
        "then",
        "they",
        "them",
        "there",
        "these",
        "those",
        "must",
        "should",
        "will",
        "answer",
        "provided",
        "context",
    }
)


def _terms(text: str) -> set[str]:
    import re

    return {
        token for token in re.findall(r"[a-z0-9]{3,}", text.casefold()) if token not in _STOPWORDS
    }


def _sentences(text: str) -> list[str]:
    import re

    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def create_llm_provider(settings: Settings) -> LLMProvider:
    """Build the configured adapter without connecting to it during startup."""

    api_key = settings.llm_api_key.get_secret_value()
    if settings.llm_provider == "mock":
        return MockGeneralLLM()
    if settings.llm_provider == "extractive":
        return ExtractiveGroundedLLM()
    if settings.llm_provider == "local":
        return LocalOpenAICompatibleLLM(
            base_url=settings.local_llm_base_url,
            api_key=api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            capabilities=LLMCapabilities(
                streaming=settings.local_llm_streaming,
                structured_output=settings.local_llm_structured_output,
                tool_calling=settings.local_llm_tool_calling,
            ),
        )
    return RemoteOpenAICompatibleLLM(
        base_url=settings.llm_base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
