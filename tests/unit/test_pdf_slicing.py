"""PDF 页数检测、页级拆分与双限分片规划（ADR-037）的离线单测。

样本用 pypdf 现场构造（空白页 PDF），不依赖远端 provider、不消耗解析额度。
"""

from __future__ import annotations

import io
from itertools import pairwise

import pytest
from pypdf import PdfReader, PdfWriter

from backend.infrastructure.parser_errors import ParserError
from backend.infrastructure.pdf_slicing import (
    PageRange,
    count_pages,
    plan_shards,
    slice_pages,
)


def _blank_pdf(pages: int, *, page_bytes: int | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if page_bytes is not None:
        writer.add_metadata({"/Padding": "x" * page_bytes})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _encrypted_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- count_pages -----------------------------------------------------------


def test_count_pages_reads_the_real_page_count() -> None:
    assert count_pages(_blank_pdf(3)) == 3
    assert count_pages(_blank_pdf(1)) == 1


def test_count_pages_rejects_empty_document() -> None:
    with pytest.raises(ParserError) as error:
        count_pages(b"")
    assert str(error.value) == "empty_document"


def test_count_pages_rejects_damaged_document() -> None:
    with pytest.raises(ParserError) as error:
        count_pages(b"%PDF-1.7\nthis is not a pdf body\n%%EOF")
    assert str(error.value) == "pdf_damaged"


def test_count_pages_rejects_encrypted_document() -> None:
    with pytest.raises(ParserError) as error:
        count_pages(_encrypted_pdf(2))
    assert str(error.value) == "pdf_encrypted"


# --- slice_pages -----------------------------------------------------------


def test_slice_pages_keeps_only_the_requested_closed_range() -> None:
    data = _blank_pdf(5)
    sliced = slice_pages(data, 2, 4)
    reader = PdfReader(io.BytesIO(sliced))
    assert len(reader.pages) == 3


def test_slice_pages_single_page_range() -> None:
    sliced = slice_pages(_blank_pdf(5), 5, 5)
    assert len(PdfReader(io.BytesIO(sliced)).pages) == 1


def test_slice_pages_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        slice_pages(_blank_pdf(3), 2, 4)
    with pytest.raises(ValueError):
        slice_pages(_blank_pdf(3), 0, 1)


def test_slice_pages_surfaces_encrypted_documents() -> None:
    with pytest.raises(ParserError) as error:
        slice_pages(_encrypted_pdf(2), 1, 1)
    assert str(error.value) == "pdf_encrypted"


def test_page_range_is_one_based_and_inclusive() -> None:
    page_range = PageRange(1, 200)
    assert page_range.page_count == 200
    assert str(page_range) == "1-200"
    with pytest.raises(ValueError):
        PageRange(0, 1)
    with pytest.raises(ValueError):
        PageRange(5, 4)


# --- plan_shards: 页数边界 -------------------------------------------------


def test_plan_shards_splits_the_313_page_book_into_1_200_and_201_313() -> None:
    # 验收目标：313 页全书优先切成 1–200、201–313。
    shards = plan_shards(313, 11_084_397, max_pages=200, max_bytes=200_000_000)
    assert shards == [PageRange(1, 200), PageRange(201, 313)]


def test_plan_shards_exactly_at_the_page_limit_is_one_shard() -> None:
    assert plan_shards(200, 5_000_000, max_pages=200, max_bytes=200_000_000) == [PageRange(1, 200)]


def test_plan_shards_one_page_over_the_limit_adds_a_single_page_shard() -> None:
    shards = plan_shards(201, 5_000_000, max_pages=200, max_bytes=200_000_000)
    assert shards == [PageRange(1, 200), PageRange(201, 201)]


def test_plan_shards_is_deterministic_and_ordered() -> None:
    first = plan_shards(313, 11_084_397, max_pages=200, max_bytes=200_000_000)
    second = plan_shards(313, 11_084_397, max_pages=200, max_bytes=200_000_000)
    assert first == second
    assert [shard.first_page for shard in first] == sorted(shard.first_page for shard in first)


def test_plan_shards_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        plan_shards(0, 100, max_pages=200, max_bytes=200)
    with pytest.raises(ValueError):
        plan_shards(10, 100, max_pages=0, max_bytes=200)
    with pytest.raises(ValueError):
        plan_shards(10, 100, max_pages=200, max_bytes=200, page_bytes=[1, 2])


# --- plan_shards: 字节边界 -------------------------------------------------


def test_plan_shards_bisects_a_page_range_over_the_byte_limit() -> None:
    # 每页 10 字节、10 页共 100 字节；字节上限 35 → 需要继续细分到每片 ≤35 字节。
    shards = plan_shards(10, 100, max_pages=10, max_bytes=35, page_bytes=[10] * 10)
    assert sum(shard.page_count for shard in shards) == 10
    for shard in shards:
        assert shard.page_count * 10 <= 35
    # 覆盖完整、无重叠。
    assert shards[0].first_page == 1
    assert shards[-1].last_page == 10
    for left, right in pairwise(shards):
        assert left.last_page + 1 == right.first_page


def test_plan_shards_fails_when_a_single_page_exceeds_the_byte_limit() -> None:
    with pytest.raises(ParserError) as error:
        plan_shards(3, 300, max_pages=200, max_bytes=50, page_bytes=[10, 200, 10])
    assert str(error.value) == "shard_too_large"


def test_plan_shards_estimates_bytes_proportionally_without_page_sizes() -> None:
    # 总 1000 字节 / 10 页 → 每页约 100 字节；上限 250 字节 → 每片最多 2 页。
    shards = plan_shards(10, 1000, max_pages=10, max_bytes=250)
    assert all(shard.page_count <= 2 for shard in shards)
    assert sum(shard.page_count for shard in shards) == 10


# --- plan_shards: 短尾片 ---------------------------------------------------


def test_plan_shards_merges_a_short_tail_when_it_fits() -> None:
    # 205 页、上限 200 → 尾片 5 页；min_pages=5 且 200+5 > 200，因此不合并。
    shards = plan_shards(205, 1_000, max_pages=200, max_bytes=10_000, min_pages=5)
    assert [shard.page_count for shard in shards] == [200, 5]


def test_plan_shards_never_exceeds_the_page_limit_to_merge() -> None:
    shards = plan_shards(401, 1_000, max_pages=200, max_bytes=10_000, min_pages=10)
    assert all(shard.page_count <= 200 for shard in shards)
    assert sum(shard.page_count for shard in shards) == 401
