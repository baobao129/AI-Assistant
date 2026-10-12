"""受控 BGE Rerank HTTP 适配器。

适配器只返回候选下标与分数，调用方负责固定候选顺序并再次执行授权过滤。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from backend.infrastructure.config import Settings

logger = logging.getLogger(__name__)


class RerankerError(RuntimeError):
    """Rerank provider unavailable or returned an invalid bounded response."""


class Reranker(Protocol):
    model: str

    def rerank(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[tuple[int, float]]: ...


@dataclass
class SiliconFlowReranker:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 30.0

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        if not self.api_key:
            raise RerankerError("reranker_missing_credentials")
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": min(max(1, top_n), len(documents)),
            "return_documents": False,
        }
        try:
            for attempt in range(2):
                response = httpx.post(
                    f"{self.base_url.rstrip('/')}/rerank",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                if response.status_code not in {429, 500, 502, 503, 504} or attempt == 1:
                    break
                time.sleep(0.25)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise RerankerError("reranker_invalid_response")
            raw = body.get("results")
            if not isinstance(raw, list) or len(raw) != payload["top_n"]:
                raise RerankerError("reranker_invalid_response")
            seen: set[int] = set()
            output: list[tuple[int, float]] = []
            for item in raw:
                if not isinstance(item, dict):
                    raise RerankerError("reranker_invalid_response")
                index = item.get("index")
                score = item.get("relevance_score")
                if isinstance(index, bool) or not isinstance(index, int):
                    raise RerankerError("reranker_invalid_index")
                if index < 0 or index >= len(documents) or index in seen:
                    raise RerankerError("reranker_invalid_index")
                if isinstance(score, bool) or not isinstance(score, (int, float)):
                    raise RerankerError("reranker_invalid_score")
                score = float(score)
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise RerankerError("reranker_invalid_score")
                seen.add(index)
                output.append((index, score))
            if not output and documents:
                raise RerankerError("reranker_empty_response")
            return sorted(output, key=lambda pair: (-pair[1], pair[0]))
        except RerankerError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            logger.warning("reranker_request_failed: %s", type(error).__name__)
            raise RerankerError("reranker_unavailable") from error


class DisabledReranker:
    model = "disabled"
    disabled = True

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        raise RerankerError("reranker_disabled")


def create_reranker(settings: Settings) -> Reranker:
    if not settings.rerank_enabled or settings.embedding_provider == "deterministic":
        return DisabledReranker()
    return SiliconFlowReranker(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.rerank_model,
        timeout_seconds=settings.rerank_timeout_seconds,
    )
