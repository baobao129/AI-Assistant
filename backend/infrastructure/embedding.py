"""Embedding Provider boundary (ADR-007 / PROJECT_SPEC §8).

Business code depends on ``EmbeddingProvider`` only. Provider SDK types and HTTP
details stay inside the adapters, and provider failures fail the caller instead of
silently degrading to less trustworthy vectors.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

import httpx

from backend.infrastructure.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingProviderError(RuntimeError):
    """The provider could not produce embeddings for the requested input."""


class EmbeddingProvider(Protocol):
    provider_name: str
    model: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class SiliconFlowEmbeddingProvider:
    """Remote OpenAI-compatible embedding adapter for the SiliconFlow MVP provider."""

    provider_name = "siliconflow"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        timeout_seconds: float = 30.0,
        batch_size: int = 32,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.batch_size = max(1, batch_size)

    def embed(self, text: str) -> list[float]:
        vectors = self.embed_many([text])
        if not vectors:
            raise EmbeddingProviderError("embedding_provider_returned_no_vector")
        return vectors[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingProviderError("embedding_provider_missing_credentials")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._request(texts[start : start + self.batch_size]))
        if len(vectors) != len(texts):
            raise EmbeddingProviderError("embedding_provider_returned_wrong_count")
        self.dimension = len(vectors[0])
        return vectors

    def _request(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch, "encoding_format": "float"}
        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            ordered = sorted(body["data"], key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            logger.warning("embedding_request_failed: %s", type(error).__name__)
            raise EmbeddingProviderError("embedding_provider_unavailable") from error
        if not vectors or not vectors[0]:
            raise EmbeddingProviderError("embedding_provider_returned_empty_vector")
        return vectors


class DeterministicEmbeddingProvider:
    """Offline provider used by tests and local smoke runs.

    It is explicit opt-in configuration (``EMBEDDING_PROVIDER=deterministic``) and
    never silently replaces the remote provider.
    """

    provider_name = "deterministic"

    def __init__(self, *, model: str = "deterministic-hash", dimension: int = 256) -> None:
        self.model = model
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        values: list[float] = []
        seed = text.encode("utf-8")
        counter = 0
        while len(values) < self.dimension:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for index in range(0, len(digest), 2):
                if len(values) >= self.dimension:
                    break
                raw = int.from_bytes(digest[index : index + 2], "big") / 65535.0
                values.append(raw * 2.0 - 1.0)
            counter += 1
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Build the configured adapter without performing any network work."""

    if settings.embedding_provider == "deterministic":
        return DeterministicEmbeddingProvider(dimension=min(settings.embedding_dimension, 256))
    return SiliconFlowEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout_seconds=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
    )
