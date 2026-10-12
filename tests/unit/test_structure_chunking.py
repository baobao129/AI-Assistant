"""结构化分块测试（ADR-038 决策 3/4）：章节路径、真实页偏移、超长块与表格保留。"""

from __future__ import annotations

from backend.infrastructure.parsers import DocumentBlock
from backend.infrastructure.structure_chunking import (
    chunk_blocks,
    content_hash,
    with_page_offset,
)
from backend.infrastructure.token_budget import estimate_tokens


def _block(
    text: str,
    *,
    block_type: str = "paragraph",
    page: int | None = 0,
    level: int | None = None,
    order: int = 0,
) -> DocumentBlock:
    return DocumentBlock(
        text=text, block_type=block_type, page_index=page, heading_level=level, order=order
    )


def _book() -> list[DocumentBlock]:
    return [
        _block("第一章 概述", block_type="heading", level=1, page=0, order=0),
        _block("第一节 背景", block_type="heading", level=2, page=0, order=1),
        _block("背景段落一。" * 10, page=0, order=2),
        _block("背景段落二。" * 10, page=1, order=3),
        _block("第二节 目标", block_type="heading", level=2, page=2, order=4),
        _block("目标段落。" * 10, page=2, order=5),
        _block("| a | b |\n| - | - |\n| 1 | 2 |", block_type="table", page=3, order=6),
    ]


# --- 页偏移 ---------------------------------------------------------------


def test_page_offset_makes_relative_pages_absolute_and_one_based():
    blocks = [_block("a", page=0), _block("b", page=34)]
    shifted = with_page_offset(blocks, page_offset=200)  # 分片 201-313
    assert [block.page_index for block in shifted] == [201, 235]


def test_page_offset_never_invents_a_page_when_there_is_no_range():
    blocks = [_block("a", page=3), _block("b", page=None)]
    shifted = with_page_offset(blocks, page_offset=None)
    assert [block.page_index for block in shifted] == [None, None]


# --- 章节路径 -------------------------------------------------------------


def test_section_path_follows_the_heading_stack():
    chunked = chunk_blocks(_book(), parent_tokens=10000, child_tokens=10000, overlap_tokens=0)
    paths = [parent.section_path for parent in chunked.parents]
    # 每个 Parent 的路径是它**起始块**所在位置的标题栈：一级标题下的分组只有一级标题。
    assert paths[0] == "第一章 概述"
    # 第二个一级标题（第一章下的第二节）开启新分组时，栈里仍带着第一章。
    assert "第一章 概述 / 第二节 目标" in paths
    for child in chunked.children:
        assert child.section_path is not None


def test_children_after_a_subheading_carry_the_subheading_path():
    blocks = [
        _block("第一章", block_type="heading", level=1, order=0),
        _block("正文一" * 20, page=0, order=1),
        _block("第一节", block_type="heading", level=2, order=2),
        _block("正文二" * 20, page=1, order=3),
    ]
    chunked = chunk_blocks(blocks, parent_tokens=10000, child_tokens=40, overlap_tokens=0)
    paths = {child.section_path for child in chunked.children}
    assert "第一章" in paths
    assert "第一章 / 第一节" in paths


def test_section_path_is_absent_without_headings():
    chunked = chunk_blocks(
        [_block("只有正文，没有标题。")], parent_tokens=100, child_tokens=100, overlap_tokens=0
    )
    assert chunked.parents[0].section_path is None


def test_section_depth_and_length_are_bounded():
    blocks = [
        _block(f"第{index}层标题" + "很长" * 40, block_type="heading", level=index)
        for index in range(1, 7)
    ]
    blocks.append(_block("正文"))
    chunked = chunk_blocks(
        blocks, parent_tokens=10000, child_tokens=10000, overlap_tokens=0, max_section_depth=3
    )
    path = chunked.children[0].section_path or ""
    assert path.count(" / ") <= 2  # 最多 3 层
    assert len(path) <= 300


# --- 真实页范围 -----------------------------------------------------------


def test_chunk_pages_come_from_the_blocks_it_actually_covers():
    chunked = chunk_blocks(
        [_block("第一页内容" * 5, page=10), _block("第二页内容" * 5, page=11)],
        parent_tokens=10000,
        child_tokens=10000,
        overlap_tokens=0,
    )
    parent = chunked.parents[0]
    assert (parent.page_start, parent.page_end) == (10, 11)


def test_chunks_without_page_information_report_none():
    chunked = chunk_blocks(
        [_block("无页码内容" * 5, page=None)], parent_tokens=100, child_tokens=100, overlap_tokens=0
    )
    assert chunked.parents[0].page_start is None
    assert chunked.parents[0].page_end is None
    assert chunked.children[0].page_start is None


# --- 结构与预算 -----------------------------------------------------------


def test_headings_start_new_parents():
    chunked = chunk_blocks(_book(), parent_tokens=100000, child_tokens=100000, overlap_tokens=0)
    # 三个标题 → 三个 Parent（每个标题开启一个章节分组）。
    assert len(chunked.parents) == 3
    assert chunked.parents[0].content.startswith("第一章 概述")


def test_oversized_single_block_is_split_so_no_parent_is_unbounded():
    huge = _block("超长段落" * 4000)
    chunked = chunk_blocks([huge], parent_tokens=200, child_tokens=100, overlap_tokens=0)
    assert len(chunked.parents) > 1
    for parent in chunked.parents:
        assert parent.token_count <= 200


def test_children_stay_within_the_child_token_budget():
    chunked = chunk_blocks(_book(), parent_tokens=10000, child_tokens=80, overlap_tokens=20)
    assert chunked.children
    for child in chunked.children:
        assert estimate_tokens(child.content) <= 80
        assert child.token_count == estimate_tokens(child.content)


def test_every_parent_maps_to_at_least_one_child_and_children_reference_real_parents():
    chunked = chunk_blocks(_book(), parent_tokens=300, child_tokens=80, overlap_tokens=20)
    ordinals = {parent.ordinal for parent in chunked.parents}
    for parent in chunked.parents:
        assert parent.child_ordinals
    for child in chunked.children:
        assert child.parent_ordinal in ordinals


def test_table_blocks_keep_their_block_type():
    chunked = chunk_blocks(
        [_block("| a | b |", block_type="table", page=3)],
        parent_tokens=1000,
        child_tokens=1000,
        overlap_tokens=0,
    )
    assert chunked.parents[0].block_type == "table"
    assert chunked.children[0].block_type == "table"


def test_code_and_image_blocks_keep_their_block_type():
    blocks = [
        _block("代码", block_type="heading", level=1, order=0),
        _block("print(1)", block_type="code", page=1, order=1),
        _block("插图", block_type="heading", level=1, order=2),
        _block("图 1 架构", block_type="image", page=2, order=3),
    ]
    chunked = chunk_blocks(blocks, parent_tokens=1000, child_tokens=1000, overlap_tokens=0)
    types = {parent.block_type for parent in chunked.parents}
    assert types == {"code", "image"}


def test_heading_only_groups_report_heading_as_the_dominant_type():
    chunked = chunk_blocks(
        [_block("第一章", block_type="heading", level=1)],
        parent_tokens=1000,
        child_tokens=1000,
        overlap_tokens=0,
    )
    assert chunked.parents[0].block_type == "heading"


def test_content_hash_is_stable_and_content_addressed():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64


def test_invalid_budgets_are_rejected():
    import pytest

    with pytest.raises(ValueError):
        chunk_blocks([_block("x")], parent_tokens=0, child_tokens=10, overlap_tokens=0)
    with pytest.raises(ValueError):
        chunk_blocks([_block("x")], parent_tokens=10, child_tokens=0, overlap_tokens=0)
