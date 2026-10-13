from __future__ import annotations

from backend.graph.main import RAGNodes
from backend.infrastructure.config import Settings
from backend.infrastructure.parsers import DocumentBlock
from backend.infrastructure.structure_chunking import chunk_blocks, with_page_offset
from backend.infrastructure.token_budget import estimate_tokens
from scripts.review_answers import grade_answer, original_pages


def test_grading_uses_original_pages_and_separate_invocation() -> None:
    class Reviewer:
        def __init__(self):
            self.prompts = []

        def generate(self, prompt, state):
            self.prompts.append((prompt, state))
            return (
                '{"required":[{"fact_index":0,"present":true,"supported":true,'
                '"citation_indices":[0],"reason":"page evidence"}],"additional":[],'
                '"citations":[{"citation_index":0,"valid":true,"reason":"correct page"}],'
                '"safe_refusal":false,"ambiguous":false}'
            )

    reviewer = Reviewer()
    question = {
        "question": "问题",
        "required_facts": ["事实"],
        "source_quotes": [r"原文 tool\_call\_id"],
    }
    answer = {"final_answer": "答案", "citations": [{"page_start": 1}], "refused": False}
    pages = original_pages(question, answer, ["原始页面事实：原文 tool_call_id"])
    grade = grade_answer(reviewer, question, answer, pages)
    assert grade["status"] == "scored"
    assert len(reviewer.prompts) == 1
    assert "原始页面事实" in reviewer.prompts[0][0]
    assert "production answer validator" in reviewer.prompts[0][0]
    assert reviewer.prompts[0][1] == {}


def test_repaired_chapter_metadata_reaches_graph_citations() -> None:
    blocks = with_page_offset(
        [
            DocumentBlock(
                text="第五章", block_type="heading", page_index=0, heading_level=1, order=0
            ),
            DocumentBlock(
                text="第8章：新章", block_type="heading", page_index=7, heading_level=3, order=1
            ),
            DocumentBlock(
                text="新章事实", block_type="paragraph", page_index=8, heading_level=None, order=2
            ),
        ],
        page_offset=200,
    )
    child = chunk_blocks(blocks, parent_tokens=1000, child_tokens=1000, overlap_tokens=0).children[
        -1
    ]
    citation = {
        "section_path": child.section_path,
        "page_start": child.page_start,
        "page_end": child.page_end,
    }
    nodes = RAGNodes(tools=None, settings=Settings(_env_file=None), general_llm=None)
    result = nodes.context_build(
        {
            "retrieval_results": [
                {"chunk_id": "c1", "content": child.content, **citation, "citation": citation}
            ]
        }
    )
    assert result["citations"] == [citation]
    assert citation == {"section_path": "第8章：新章", "page_start": 208, "page_end": 209}
    assert "第8章：新章" in result["context"]


def test_cross_chapter_answer_uses_only_selected_bounded_evidence() -> None:
    class Generator:
        def generate(self, prompt, state):
            assert "第五章事实" in prompt and "第八章事实" in prompt
            assert "未检索章节" not in prompt
            return "第五章事实[c1]，第八章事实[c2]。"

    nodes = RAGNodes(
        tools=None,
        settings=Settings(_env_file=None, llm_provider="mock", max_context_tokens=200),
        general_llm=Generator(),
    )  # type: ignore[arg-type]
    result = nodes.context_build(
        {
            "retrieval_results": [
                {
                    "chunk_id": "c1",
                    "content": "第五章事实",
                    "section_path": "第五章",
                    "page_start": 5,
                },
                {
                    "chunk_id": "c2",
                    "content": "第八章事实",
                    "section_path": "第八章",
                    "page_start": 8,
                },
            ]
        }
    )
    assert "第五章事实" in result["context"]
    assert "第八章事实" in result["context"]
    assert "未检索章节" not in result["context"]
    assert estimate_tokens(result["context"]) <= 200
    generated = nodes.generation({**result, "user_input": "跨章对比"})
    assert generated["answer_candidate"] == "第五章事实[c1]，第八章事实[c2]。"
