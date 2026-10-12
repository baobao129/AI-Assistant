import math

import httpx
import pytest

from backend.infrastructure.reranker import RerankerError, SiliconFlowReranker


def _reranker(monkeypatch, results):
    def post(url, **kwargs):
        assert url == "https://example.test/v1/rerank"
        assert kwargs["json"]["documents"] == ["authorized one", "authorized two"]
        return httpx.Response(200, json={"results": results}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    return SiliconFlowReranker("https://example.test/v1", "test-only", "BAAI/bge-reranker-v2-m3")


def test_valid_rerank_preserves_input_identity(monkeypatch):
    provider = _reranker(monkeypatch, [{"index": 1, "relevance_score": 0.9}])
    assert provider.rerank("query", ["authorized one", "authorized two"], top_n=1) == [(1, 0.9)]


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": -1, "relevance_score": 0.2}],
        [{"index": 2, "relevance_score": 0.2}],
        [{"index": True, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.3}],
        [{"index": 0, "relevance_score": "NaN"}],
        [{"index": 0, "relevance_score": math.inf}],
        [{"index": 0}],
    ],
)
def test_invalid_rerank_responses_fail_safely(monkeypatch, results):
    provider = _reranker(monkeypatch, results)
    with pytest.raises(RerankerError):
        provider.rerank("query", ["authorized one", "authorized two"], top_n=2)


def test_provider_timeout_is_safe(monkeypatch):
    def post(*args, **kwargs):
        raise httpx.ReadTimeout("sensitive provider diagnostic")

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(RerankerError, match="^reranker_unavailable$"):
        SiliconFlowReranker("https://example.test", "test-only", "model").rerank(
            "query", ["one"], top_n=1
        )
