"""结构化分块（ADR-038 决策 3/4）：按章节与连续块建 Parent，按 token 预算切 Child。

输入是 provider 提供的结构化块（``DocumentBlock``），输出与文本回退路径**同一个**
``ChunkedDocument`` 类型，因此摄取层只需要换一个调用，不需要两套下游逻辑。

三条诚实规则：

- **页码不伪造**：块没有可靠页号时，该块的页范围为 ``None``；整块都没有页号时，
  chunk 的 ``page_start``/``page_end`` 为 ``None``，绝不写 1。
- **标题不猜测**：章节路径只来自 provider 的标题层级（``text_level``），没有标题就是
  没有章节路径（``None``），不从字体或空行推断。
- **预算是 token 不是字符**：Parent/Child 的切分都用 :mod:`token_budget` 的保守估计，
  因此中文与英文按各自的比例计费，绝不把中文字符数等同 token 数。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace

from backend.infrastructure.parsers import ChildChunk, ChunkedDocument, DocumentBlock, ParentChunk
from backend.infrastructure.token_budget import estimate_tokens, truncate_to_tokens

__all__ = ["chunk_blocks", "content_hash", "with_page_offset"]

# 章节路径的最大层级与最大长度：State 与检索结果都必须有界（STATE_CONTRACT 规则 9）。
DEFAULT_MAX_SECTION_DEPTH = 4
DEFAULT_SECTION_PATH_CHARS = 300


def with_page_offset(
    blocks: Sequence[DocumentBlock], *, page_offset: int | None
) -> tuple[DocumentBlock, ...]:
    """把 provider 的**相对**页号换算为**绝对** 1-based 页码。

    ``page_offset`` 是该分片的 ``page_start - 1``（分片第 0 页对应的绝对 0-based 页号）。
    没有页号、或分片没有页范围（非 PDF）时返回 ``None``，**绝不伪造页码**。
    """

    if page_offset is None:
        return tuple(replace(block, page_index=None) for block in blocks)
    return tuple(
        replace(block, page_index=block.page_index + page_offset + 1)
        if block.page_index is not None
        else block
        for block in blocks
    )


def content_hash(text: str) -> str:
    """分块内容的 sha256（顺序与去重判据，绝不依赖 ordinal 的偶然稳定）。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_blocks(
    blocks: Sequence[DocumentBlock],
    *,
    parent_tokens: int,
    child_tokens: int,
    overlap_tokens: int,
    max_section_depth: int = DEFAULT_MAX_SECTION_DEPTH,
    section_path_chars: int = DEFAULT_SECTION_PATH_CHARS,
) -> ChunkedDocument:
    """把结构化块切分为有界的 Parent 与 Child。

    - 标题块开启新的 Parent（章节边界）；
    - Parent 累计 token 超过 ``parent_tokens`` 时关闭；单个超长块按预算硬切，
      保证没有任何无界 Parent；
    - Child 在 Parent 内按 ``child_tokens`` 开窗、``overlap_tokens`` 重叠；
    - 每个 chunk 的页范围来自它**实际覆盖的块**，没有页号就是 ``None``。
    """

    if parent_tokens <= 0 or child_tokens <= 0:
        raise ValueError("chunk token budgets must be positive")
    if max_section_depth <= 0 or section_path_chars <= 0:
        raise ValueError("section path bounds must be positive")
    overlap = max(0, min(overlap_tokens, child_tokens // 2))

    paths = _section_paths(blocks, max_depth=max_section_depth, max_chars=section_path_chars)
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    child_ordinal = 0
    for group in _group_parents(blocks, parent_tokens=parent_tokens):
        content, spans = _join_blocks([block for _, block in group])
        if not content.strip():
            continue
        section_path = paths[group[0][0]]
        parent_ordinal = len(parents)
        child_ordinals: list[int] = []
        for start, end in _token_windows(content, size=child_tokens, overlap=overlap):
            piece = content[start:end]
            if not piece.strip():
                continue
            covered = [
                block
                for span_start, span_end, block in spans
                if span_start < end and span_end > start
            ]
            child_ordinals.append(child_ordinal)
            children.append(
                ChildChunk(
                    ordinal=child_ordinal,
                    parent_ordinal=parent_ordinal,
                    content=piece,
                    section_path=section_path,
                    block_type=_dominant_type(covered),
                    page_start=_page_bound(covered, lowest=True),
                    page_end=_page_bound(covered, lowest=False),
                    token_count=estimate_tokens(piece),
                )
            )
            child_ordinal += 1
        if not child_ordinals:
            continue
        group_blocks = [block for _, block in group]
        parents.append(
            ParentChunk(
                ordinal=parent_ordinal,
                content=content,
                child_ordinals=tuple(child_ordinals),
                section_path=section_path,
                block_type=_dominant_type(group_blocks),
                page_start=_page_bound(group_blocks, lowest=True),
                page_end=_page_bound(group_blocks, lowest=False),
                token_count=estimate_tokens(content),
            )
        )
    return ChunkedDocument(parents=tuple(parents), children=tuple(children))


# --- 内部实现 ---------------------------------------------------------------


def _bounded_title(text: str, *, max_chars: int) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return first_line[:max_chars]


def _section_paths(
    blocks: Sequence[DocumentBlock], *, max_depth: int, max_chars: int
) -> list[str | None]:
    """按文档顺序推进标题栈，给出每个块的章节路径（没有标题就是 ``None``）。"""

    stack: list[tuple[int, str]] = []
    paths: list[str | None] = []
    for block in blocks:
        if block.block_type == "heading" and block.heading_level:
            level = int(block.heading_level)
            title = _bounded_title(block.text, max_chars=max_chars)
            # Some parser headings use inconsistent levels across PDF shards.
            # An explicitly numbered chapter is a peer of the preceding chapter;
            # it cannot inherit that chapter merely because text_level increased.
            if re.match(r"^第\s*[0-9一二三四五六七八九十百]+\s*章(?:\s|[：:、]|$)", title):
                preceding = next(
                    (
                        depth
                        for depth, text in stack
                        if re.match(
                            r"^第\s*[0-9一二三四五六七八九十百]+\s*章(?:\s|[：:、]|$)", text
                        )
                    ),
                    None,
                )
                if preceding is not None:
                    level = preceding
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        titles = [title for _, title in stack[:max_depth]]
        path = " / ".join(title for title in titles if title)
        paths.append(path[:max_chars] if path else None)
    return paths


def _group_parents(
    blocks: Sequence[DocumentBlock], *, parent_tokens: int
) -> list[list[tuple[int, DocumentBlock]]]:
    """按「标题开启新章节」与「Parent token 预算」分组。

    返回 ``[(原始块序号, 块), ...]`` 分组：超长单块会被替换成按预算切出的片段，
    因此分组必须携带块本身而不只是序号。
    """

    groups: list[list[tuple[int, DocumentBlock]]] = []
    current: list[tuple[int, DocumentBlock]] = []
    current_tokens = 0
    for index, block in enumerate(blocks):
        tokens = estimate_tokens(block.text)
        starts_section = block.block_type == "heading"
        if current and (starts_section or current_tokens + tokens > parent_tokens):
            groups.append(current)
            current = []
            current_tokens = 0
        if tokens > parent_tokens:
            # 超长单块：按 Parent 预算硬切，保证 Parent 有界。
            for piece in _split_by_tokens(block.text, parent_tokens):
                groups.append([(index, replace(block, text=piece))])
            continue
        current.append((index, block))
        current_tokens += tokens
    if current:
        groups.append(current)
    return groups


def _split_by_tokens(text: str, budget: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while remaining:
        piece, truncated = truncate_to_tokens(remaining, budget)
        if not piece:
            break
        pieces.append(piece)
        if not truncated:
            break
        remaining = remaining[len(piece) :]
    return pieces


def _join_blocks(
    blocks: Sequence[DocumentBlock],
) -> tuple[str, list[tuple[int, int, DocumentBlock]]]:
    """把块拼成 Parent 正文，同时记录每个块在正文中的字符跨度（用于精确页范围）。"""

    parts: list[str] = []
    spans: list[tuple[int, int, DocumentBlock]] = []
    cursor = 0
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if cursor:
            parts.append("\n\n")
            cursor += 2
        parts.append(text)
        spans.append((cursor, cursor + len(text), block))
        cursor += len(text)
    return "".join(parts), spans


def _token_windows(text: str, *, size: int, overlap: int) -> list[tuple[int, int]]:
    """按 token 预算在字符空间开窗；返回 ``[(start, end), ...]``。"""

    windows: list[tuple[int, int]] = []
    start = 0
    total = len(text)
    while start < total:
        piece, _ = truncate_to_tokens(text[start:], size)
        if not piece:
            break
        end = start + len(piece)
        windows.append((start, end))
        if end >= total:
            break
        back = _suffix_length_for_tokens(text, end, overlap)
        start = max(start + 1, end - back)
    return windows


def _suffix_length_for_tokens(text: str, end: int, budget: int) -> int:
    if budget <= 0 or end <= 0:
        return 0
    low, high = 0, end
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[end - middle : end]) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def _dominant_type(blocks: Sequence[DocumentBlock]) -> str:
    """块的支配类型：优先非标题类型（标题只描述章节，不描述证据形态）。"""

    counts: dict[str, int] = {}
    for block in blocks:
        if block.block_type == "heading":
            continue
        counts[block.block_type] = counts.get(block.block_type, 0) + 1
    if not counts:
        return "heading" if blocks else "paragraph"
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _page_bound(blocks: Sequence[DocumentBlock], *, lowest: bool) -> int | None:
    pages = [block.page_index for block in blocks if block.page_index is not None]
    if not pages:
        return None
    return min(pages) if lowest else max(pages)
