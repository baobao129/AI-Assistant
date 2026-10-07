"""Parser 适配器与 Parent/Child 分块测试。"""

import pytest

from backend.infrastructure.parsers import (
    LightweightParser,
    ParserError,
    chunk_document,
    supported_content_type,
)


def parse_markdown(text: str):
    return LightweightParser(max_bytes=1024 * 1024).parse(
        filename="policy.md", content_type="text/markdown", data=text.encode("utf-8")
    )


def test_lightweight_parser_normalizes_text():
    parsed = parse_markdown("Title\n\n\n\nBody   with   spaces\n")
    assert parsed.parser == "lightweight"
    assert "Body with spaces" in parsed.text
    assert "\n\n\n" not in parsed.text
    assert parsed.truncated is False


def test_lightweight_parser_rejects_empty_and_oversized_documents():
    parser = LightweightParser(max_bytes=16)
    with pytest.raises(ParserError):
        parser.parse(filename="a.txt", content_type="text/plain", data=b"")
    with pytest.raises(ParserError):
        parser.parse(filename="a.txt", content_type="text/plain", data=b"x" * 64)


def test_lightweight_parser_rejects_binary_content():
    with pytest.raises(ParserError):
        LightweightParser(max_bytes=1024).parse(
            filename="a.txt", content_type="text/plain", data=b"\xff\xfe\x00\x01"
        )


def test_csv_and_json_are_converted_to_text():
    parser = LightweightParser(max_bytes=4096)
    csv_document = parser.parse(filename="rows.csv", content_type="text/csv", data=b"a,b\n1,2\n")
    assert "a | b" in csv_document.text
    json_document = parser.parse(
        filename="data.json", content_type="application/json", data=b'{"k": "v"}'
    )
    assert '"k": "v"' in json_document.text


def test_supported_content_types():
    assert supported_content_type("text/markdown", "a.md")
    assert supported_content_type("application/octet-stream", "a.txt")
    assert not supported_content_type("application/pdf", "a.pdf")


def test_chunking_creates_bounded_parents_and_overlapping_children():
    # Parent 上限约为 parent_chars 的两倍，Child 需带上重叠以满足上下文连续性。
    text = "\n\n".join(f"Paragraph {index} " + "word " * 60 for index in range(6))
    chunked = chunk_document(text, parent_chars=900, child_chars=300, overlap_chars=60)
    assert chunked.parents, "parents must be produced"
    assert chunked.children, "children must be produced"
    for parent in chunked.parents:
        assert len(parent.content) <= 1800
        assert parent.child_ordinals, "every parent maps to at least one child"
    for child in chunked.children:
        assert len(child.content) <= 300
    assert {child.parent_ordinal for child in chunked.children} == {
        parent.ordinal for parent in chunked.parents
    }


def test_chunking_short_document_produces_one_parent():
    chunked = chunk_document(
        "Only one short section.", parent_chars=800, child_chars=200, overlap_chars=40
    )
    assert len(chunked.parents) == 1
    assert len(chunked.children) == 1
    assert chunked.children[0].parent_ordinal == 0


def test_chunking_hard_splits_an_oversized_paragraph():
    chunked = chunk_document("x" * 5000, parent_chars=1000, child_chars=400, overlap_chars=50)
    assert all(len(parent.content) <= 2000 for parent in chunked.parents)
    assert len(chunked.parents) >= 3
