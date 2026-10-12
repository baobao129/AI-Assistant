"""PDF 页数检测、页级拆分与双限分片规划（ADR-037）。

纯函数模块：不触碰网络、磁盘或数据库，只做「读 PDF 结构 → 产出页范围 / 字节」。
摄取层负责把计划落成 manifest 并逐片调用 provider。

三条安全约定（ADR-037）：

- 加密 PDF 明确失败 ``pdf_encrypted``，绝不尝试猜测口令或返回猜出来的页数；
- 结构损坏明确失败 ``pdf_damaged``，绝不伪造页数；
- 分片必须**同时**满足页数上限与字节上限；单页仍超字节上限时明确失败
  ``shard_too_large``，绝不无限递归拆分。

页码一律 **1-based 闭区间**，与用户可见页码一致，因此合并后的文本可以直接回查原页。
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass

from pypdf import PdfReader, PdfWriter
from pypdf.errors import DependencyError, EmptyFileError, FileNotDecryptedError, PdfReadError

from backend.infrastructure.parser_errors import ParserError

__all__ = ["PageRange", "count_pages", "plan_shards", "slice_pages"]


@dataclass(frozen=True)
class PageRange:
    """1-based 闭区间页范围（``first_page`` 与 ``last_page`` 都包含在内）。"""

    first_page: int
    last_page: int

    def __post_init__(self) -> None:
        if self.first_page < 1:
            raise ValueError("page ranges are 1-based")
        if self.last_page < self.first_page:
            raise ValueError("page range must be non-empty")

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1

    def __str__(self) -> str:
        return f"{self.first_page}-{self.last_page}"


def count_pages(data: bytes) -> int:
    """返回 PDF 的真实页数；加密/损坏/空文档给出明确安全码。"""

    return _page_count(_open_reader(data))


def slice_pages(data: bytes, first_page: int, last_page: int) -> bytes:
    """按 1-based 闭区间原样取出页面，重新序列化为一个独立 PDF。

    只复制页对象，不做任何内容改写（不重排、不缩放、不重编码），因此 provider 解析
    出来的页码可以直接映射回原文件页码。
    """

    page_range = PageRange(first_page, last_page)
    reader = _open_reader(data)
    total = _page_count(reader)
    if page_range.last_page > total:
        raise ValueError("page range exceeds the document page count")
    writer = PdfWriter()
    try:
        for index in range(page_range.first_page - 1, page_range.last_page):
            writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
    except FileNotDecryptedError as error:
        raise ParserError("pdf_encrypted") from error
    except (PdfReadError, EmptyFileError, OSError, ValueError) as error:
        raise ParserError("pdf_damaged") from error
    return buffer.getvalue()


def plan_shards(
    page_count: int,
    total_bytes: int,
    *,
    max_pages: int,
    max_bytes: int,
    min_pages: int = 1,
    page_bytes: Sequence[int] | None = None,
) -> list[PageRange]:
    """规划分片：先按页数上限贪心切，再对超字节上限的片二分细分。

    - ``page_bytes`` 给出逐页真实字节时按真实值判断；否则按页数比例估算
      （确定性、可复现）。真实字节在落盘时另行核算，超出上限的片在提交前失败。
    - 单页仍超 ``max_bytes`` → ``shard_too_large``（明确失败，绝不无限递归）。
    - ``min_pages`` 只影响过短尾片：能并入前一片且不突破 ``max_pages`` 时才并入，
      绝不为了合并而突破 provider 的页数硬限。
    """

    if page_count < 1:
        raise ValueError("page_count must be positive")
    if max_pages < 1 or max_bytes < 1 or min_pages < 1:
        raise ValueError("limits must be positive")
    if total_bytes < 0:
        raise ValueError("total_bytes must not be negative")
    if page_bytes is not None and len(page_bytes) != page_count:
        raise ValueError("page_bytes must describe every page")

    def size_of(page_range: PageRange) -> int:
        if page_bytes is not None:
            return sum(page_bytes[page_range.first_page - 1 : page_range.last_page])
        return math.ceil(total_bytes * page_range.page_count / page_count)

    result: list[PageRange] = []
    # 显式栈而不是递归：拆分深度有界（log2 页数），但这里仍不依赖递归上限。
    stack = list(reversed(_split_by_pages(page_count, max_pages=max_pages, min_pages=min_pages)))
    while stack:
        current = stack.pop()
        if size_of(current) <= max_bytes:
            result.append(current)
            continue
        if current.page_count == 1:
            raise ParserError("shard_too_large")
        midpoint = current.first_page + current.page_count // 2
        stack.append(PageRange(midpoint, current.last_page))
        stack.append(PageRange(current.first_page, midpoint - 1))
    result.sort(key=lambda item: item.first_page)
    return result


def _split_by_pages(page_count: int, *, max_pages: int, min_pages: int) -> list[PageRange]:
    ranges: list[PageRange] = []
    first = 1
    while first <= page_count:
        last = min(first + max_pages - 1, page_count)
        ranges.append(PageRange(first, last))
        first = last + 1
    if len(ranges) >= 2 and ranges[-1].page_count < min_pages:
        tail = ranges.pop()
        previous = ranges[-1]
        if previous.page_count + tail.page_count <= max_pages:
            ranges[-1] = PageRange(previous.first_page, tail.last_page)
        else:
            ranges.append(tail)
    return ranges


def _open_reader(data: bytes) -> PdfReader:
    if not data:
        raise ParserError("empty_document")
    try:
        reader = PdfReader(io.BytesIO(data))
    except DependencyError as error:
        # 加密算法依赖缺失：与「需要口令」同样是无法安全解析，明确失败而不是猜。
        raise ParserError("pdf_encrypted") from error
    except (PdfReadError, EmptyFileError, OSError, ValueError) as error:
        raise ParserError("pdf_damaged") from error
    if reader.is_encrypted:
        raise ParserError("pdf_encrypted")
    return reader


def _page_count(reader: PdfReader) -> int:
    try:
        count = len(reader.pages)
    except FileNotDecryptedError as error:
        raise ParserError("pdf_encrypted") from error
    except (PdfReadError, EmptyFileError, OSError, ValueError) as error:
        raise ParserError("pdf_damaged") from error
    if count < 1:
        raise ParserError("pdf_damaged")
    return count
