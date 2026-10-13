from __future__ import annotations

import json

import pytest

from backend.graph.main import RAGNodes
from backend.infrastructure.config import Settings
from backend.infrastructure.parsers import DocumentBlock
from backend.infrastructure.structure_chunking import chunk_blocks
from scripts.review_answers import normalize_grade, summarize


def _question() -> dict:
    return {
        "question": "政策是什么？",
        "required_facts": ["事实甲", "事实乙"],
        "source_quotes": ["事实甲", "事实乙"],
    }


def _answer() -> dict:
    return {"citations": [{"page_start": 1}, {"page_start": 2}], "refused": False}


def test_normalize_grade_requires_each_fact_and_citation_once() -> None:
    raw = json.dumps(
        {
            "required": [
                {
                    "fact_index": 0,
                    "present": True,
                    "supported": True,
                    "citation_indices": [0],
                    "reason": "原文",
                },
                {
                    "fact_index": 1,
                    "present": True,
                    "supported": True,
                    "citation_indices": [1],
                    "reason": "原文",
                },
            ],
            "additional": [],
            "citations": [
                {"citation_index": 0, "valid": True, "reason": "页码正确"},
                {"citation_index": 1, "valid": True, "reason": "页码正确"},
            ],
            "safe_refusal": False,
            "ambiguous": False,
        },
        ensure_ascii=False,
    )
    grade = normalize_grade(raw, _question(), _answer())
    assert all(row["supported"] and row["citation_correct"] for row in grade["facts"])

    missing = json.loads(raw)
    missing["required"].pop()
    with pytest.raises(ValueError, match="omitted"):
        normalize_grade(json.dumps(missing), _question(), _answer())
    duplicate = json.loads(raw)
    duplicate["required"][1]["fact_index"] = 0
    with pytest.raises(ValueError, match="duplicate"):
        normalize_grade(json.dumps(duplicate), _question(), _answer())
    invalid = json.loads(raw)
    invalid["required"][0]["citation_indices"] = [0, 1]
    invalid["citations"][1]["valid"] = False
    assert normalize_grade(json.dumps(invalid), _question(), _answer())["facts"][0][
        "citation_correct"
    ]
    assert not normalize_grade(json.dumps(invalid), _question(), _answer())["citations"][1]["valid"]


def test_summarize_records_numerators_denominators_and_no_go() -> None:
    records = [
        {
            "question_id": "Q-001",
            "case_kind": "retrieval",
            "hit@10": True,
            "refused": False,
            "grade": {
                "status": "scored",
                "safe_refusal": False,
                "citations": [],
                "facts": [
                    {"required": True, "present": True, "supported": True, "citation_correct": True}
                ],
            },
        },
        {
            "question_id": "Q-002",
            "case_kind": "no_answer",
            "hit@10": False,
            "refused": True,
            "grade": {"status": "scored", "safe_refusal": True, "facts": [], "citations": []},
        },
    ]
    summary = summarize(records)
    assert summary["metrics"]["hit@10"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert summary["metrics"]["no_answer_safety"]["numerator"] == 1
    assert summary["automated_gate"] == "PASS"
    assert summary["formal_release"] == "NO-GO"
    records[0]["grade"]["facts"][0]["citation_correct"] = False
    failed = summarize(records)
    assert failed["automated_gate"] == "NO-GO"
    assert failed["failed_thresholds"] == ["citation_accuracy"]
    assert failed["failure_questions"] == ["Q-001"]


def test_context_truncation_keeps_a_complete_sentence_when_available() -> None:
    settings = Settings(
        _env_file=None, llm_provider="mock", max_context_tokens=200, max_context_chars=500
    )
    nodes = RAGNodes(tools=None, settings=settings, general_llm=None)  # type: ignore[arg-type]
    complete = "第一句完整" * 25 + "。"
    context = nodes.context_build(
        {"retrieval_results": [{"chunk_id": "c1", "content": complete + "第二句会被截断" * 100}]}
    )["context"]
    assert context is not None
    assert complete in context
    assert "第二句会被截断" not in context


def test_explicit_chapter_heading_does_not_inherit_parser_level() -> None:
    blocks = [
        DocumentBlock(text="第五章", block_type="heading", page_index=0, heading_level=1, order=0),
        DocumentBlock(
            text="内容五", block_type="paragraph", page_index=0, heading_level=None, order=1
        ),
        DocumentBlock(
            text="第八章：新章", block_type="heading", page_index=1, heading_level=3, order=2
        ),
        DocumentBlock(
            text="内容八", block_type="paragraph", page_index=1, heading_level=None, order=3
        ),
    ]
    chunked = chunk_blocks(blocks, parent_tokens=1000, child_tokens=1000, overlap_tokens=0)
    assert chunked.children[-1].section_path == "第八章：新章"
