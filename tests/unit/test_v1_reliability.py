from __future__ import annotations

import httpx
import pytest

from backend.graph.validation import _has_grounding
from backend.infrastructure.llm import LLMProviderError, RemoteOpenAICompatibleLLM
from scripts.review_answers import grade_answer, original_pages


def test_original_anchor_normalizes_only_pdf_and_markdown_representation() -> None:
    pages = ["每条 tool 消息通过 tool_call_id 与对应请求关联。", "含有 oﬀ-policy 成分。"]
    question = {"source_quotes": [r"每条 tool 消息通过 tool\_call\_id", "含有 off-policy 成分"]}
    assert original_pages(question, {}, pages) == {"1": pages[0], "2": pages[1]}
    for quote in ("不含有 off-policy 成分", "通过 wrong_id", ""):
        with pytest.raises(ValueError):
            original_pages({"source_quotes": [quote]}, {}, pages)


def test_clause_polarity_keeps_supported_text_and_rejects_direct_inversion() -> None:
    evidence = [{"content": "要求每一次都通过，且不能触发安全否决项。它回答的是稳定交付。"}]
    assert _has_grounding("不能触发安全否决项，它回答的是稳定交付。", evidence)
    assert _has_grounding("要求每一次都通过，且不能触发安全否决项。", evidence)
    assert not _has_grounding("它不能回答的是稳定交付，要求每一次都通过。", evidence)
    rule = [{"content": "员工禁止提交未经审核的业务数据。"}]
    assert not _has_grounding("员工提交未经审核的业务数据。", rule)


def test_nonstream_provider_failure_categories_never_expose_response_or_credentials(monkeypatch):
    provider = RemoteOpenAICompatibleLLM(
        base_url="https://test.invalid", api_key="private-test-token", model="test"
    )
    request = httpx.Request("POST", "https://test.invalid")
    errors = [
        (httpx.ReadTimeout("private-test-token", request=request), "timeout"),
        (httpx.ConnectError("private-test-token", request=request), "transport"),
    ]
    for status, category in ((400, "http_client"), (500, "http_server")):
        response = httpx.Response(status, request=request, text="private-provider-body")
        errors.append(
            (
                httpx.HTTPStatusError("private-test-token", request=request, response=response),
                category,
            )
        )
    for error, category in errors:

        def fail(*args, failure=error, **kwargs):
            raise failure

        monkeypatch.setattr(httpx, "post", fail)
        with pytest.raises(LLMProviderError) as caught:
            provider.generate("question", {})
        assert caught.value.failure_kind == category
        assert str(caught.value) == "llm_unavailable"
        assert caught.value.__cause__ is None


def test_reviewer_timeout_stays_failed_with_fixed_facts_and_single_attempt():
    class Reviewer:
        calls = 0

        def generate(self, prompt, state):
            self.calls += 1
            raise LLMProviderError("llm_unavailable", failure_kind="timeout")

    reviewer = Reviewer()
    question = {"question": "问题", "required_facts": ["事实甲", "事实乙"], "source_quotes": []}
    grade = grade_answer(reviewer, question, {"final_answer": "答案"}, {"1": "原文"})
    assert grade["status"] == "failed" and grade["failure_kind"] == "timeout"
    assert len(grade["facts"]) == 2 and not any(f["supported"] for f in grade["facts"])
    assert reviewer.calls == grade["invocation"]["attempts"] == 1
    assert grade["invocation"]["prompt_chars"] > 0
