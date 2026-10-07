"""Parser Adapter boundary (ADR-007) and Parent/Child chunking.

MinerU is the planned formal Parser; ``LightweightParser`` keeps development and
smoke runs unblocked by extracting text from the formats the Demo uses. A parser
returns normalized text plus safe metadata and never touches Graph State.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from backend.infrastructure.config import Settings

TEXT_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "text/x-markdown",
    }
)
SUPPORTED_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".markdown", ".csv", ".json")


class ParserError(RuntimeError):
    """The document could not be normalized into text."""


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    title: str
    parser: str
    pages: int
    truncated: bool
    warnings: tuple[str, ...] = ()


class DocumentParser(Protocol):
    provider_name: str

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument: ...


class LightweightParser:
    """Development fallback parser: plain text, Markdown, CSV, and JSON."""

    provider_name = "lightweight"

    def __init__(self, *, max_bytes: int, max_chars: int = 400_000) -> None:
        self.max_bytes = max_bytes
        self.max_chars = max_chars

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_too_large")
        text = self._decode(data, content_type, filename)
        normalized = self._normalize(text)
        if not normalized.strip():
            raise ParserError("no_extractable_text")
        truncated = len(normalized) > self.max_chars
        return ParsedDocument(
            text=normalized[: self.max_chars],
            title=filename,
            parser=self.provider_name,
            pages=1,
            truncated=truncated,
            warnings=("text_truncated",) if truncated else (),
        )

    @staticmethod
    def _decode(data: bytes, content_type: str, filename: str) -> str:
        lowered = filename.casefold()
        try:
            if content_type == "application/json" or lowered.endswith(".json"):
                return json.dumps(json.loads(data.decode("utf-8")), ensure_ascii=False, indent=2)
            if content_type == "text/csv" or lowered.endswith(".csv"):
                rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
                return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ParserError("unsupported_encoding") from error
        except (ValueError, csv.Error) as error:
            raise ParserError("unparseable_document") from error

    @staticmethod
    def _normalize(text: str) -> str:
        without_controls = "".join(
            character if character.isprintable() or character in "\n\t" else " "
            for character in text
        )
        collapsed = re.sub(r"[ \t]+", " ", without_controls)
        return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


class MinerUParser:
    """Adapter for a MinerU service endpoint; deployment is out of MVP scope."""

    provider_name = "mineru"

    def __init__(self, *, base_url: str, timeout_seconds: float, max_bytes: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_too_large")
        try:
            response = httpx.post(
                f"{self.base_url}/parse",
                files={"file": (filename, data, content_type or "application/octet-stream")},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            text = body["text"]
            pages = int(body.get("pages", 1))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise ParserError("parser_unavailable") from error
        if not isinstance(text, str) or not text.strip():
            raise ParserError("no_extractable_text")
        return ParsedDocument(
            text=text,
            title=filename,
            parser=self.provider_name,
            pages=max(pages, 1),
            truncated=False,
        )


def create_parser(settings: Settings) -> DocumentParser:
    if settings.parser_provider == "mineru":
        return MinerUParser(
            base_url=settings.mineru_base_url,
            timeout_seconds=settings.parser_timeout_seconds,
            max_bytes=settings.max_document_bytes,
        )
    return LightweightParser(max_bytes=settings.max_document_bytes)


def supported_content_type(content_type: str, filename: str) -> bool:
    lowered = filename.casefold()
    return (
        content_type in TEXT_CONTENT_TYPES
        or content_type.startswith("text/")
        or lowered.endswith(SUPPORTED_SUFFIXES)
    )


@dataclass(frozen=True)
class ParentChunk:
    ordinal: int
    content: str
    child_ordinals: tuple[int, ...]


@dataclass(frozen=True)
class ChildChunk:
    ordinal: int
    parent_ordinal: int
    content: str


@dataclass(frozen=True)
class ChunkedDocument:
    parents: tuple[ParentChunk, ...]
    children: tuple[ChildChunk, ...]


def chunk_document(
    text: str, *, parent_chars: int, child_chars: int, overlap_chars: int
) -> ChunkedDocument:
    """Split normalized text into bounded Parent sections and overlapping Child chunks."""

    if parent_chars <= 0 or child_chars <= 0:
        raise ValueError("chunk sizes must be positive")
    overlap = max(0, min(overlap_chars, child_chars // 2))
    blocks = _split_blocks(text, parent_chars)
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    child_ordinal = 0
    for parent_index, block in enumerate(blocks):
        child_ordinals: list[int] = []
        for piece in _window(block, child_chars, overlap):
            child_ordinals.append(child_ordinal)
            children.append(
                ChildChunk(ordinal=child_ordinal, parent_ordinal=parent_index, content=piece)
            )
            child_ordinal += 1
        parents.append(
            ParentChunk(ordinal=parent_index, content=block, child_ordinals=tuple(child_ordinals))
        )
    return ChunkedDocument(parents=tuple(parents), children=tuple(children))


def _split_blocks(text: str, parent_chars: int) -> list[str]:
    paragraphs = [
        paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()
    ]
    if not paragraphs:
        return []
    blocks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not current:
            current = paragraph
            continue
        if len(current) + len(paragraph) + 2 <= parent_chars:
            current = f"{current}\n\n{paragraph}"
            continue
        blocks.append(current)
        current = paragraph
    if current:
        blocks.append(current)
    # A single oversized paragraph is hard-split so no Parent chunk is unbounded.
    bounded: list[str] = []
    for block in blocks:
        while len(block) > parent_chars * 2:
            bounded.append(block[: parent_chars * 2])
            block = block[parent_chars * 2 :]
        bounded.append(block)
    return [block for block in bounded if block.strip()]


def _window(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        piece = text[start : start + size]
        if piece.strip():
            pieces.append(piece)
        if start + size >= len(text):
            break
        start += step
    return pieces
