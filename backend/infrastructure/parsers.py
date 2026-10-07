"""Parser Adapter 边界（ADR-007）与 Parent/Child 分块。

``MinerUParser`` 对接 mineru.net v4 云解析 API（异步任务：预签名上传 → 轮询结果 →
下载 ZIP 取 full.md）；``LightweightParser`` 作为开发兜底解析纯文本、Markdown、CSV
与 JSON。parser 返回规范化文本与安全元数据，且绝不触碰 Graph State。
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
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

# MinerU 精准解析 API 支持的上传类型（≤200MB、≤600 页，官方文档 v4）。
MINERU_SUFFIXES: tuple[str, ...] = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
)

# 轮询间隔与终态：整体等待仍受 parser_timeout_seconds 约束，绝不无限轮询。
MINERU_POLL_INTERVAL_SECONDS = 5.0


class ParserError(RuntimeError):
    """文档无法被规范化为文本。"""


def normalize_text(text: str) -> str:
    """把解析结果规范化为可入库文本：去除控制字符、折叠空白与多余空行。"""

    without_controls = "".join(
        character if character.isprintable() or character in "\n\t" else " " for character in text
    )
    collapsed = re.sub(r"[ \t]+", " ", without_controls)
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


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
    """开发用兜底 parser：纯文本、Markdown、CSV 与 JSON。"""

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
        normalized = normalize_text(text)
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


class MinerUParser:
    """mineru.net v4 云解析适配器（异步任务：预签名上传 → 轮询 → 下载 ZIP）。

    流程依据官方 v4 文档：本地文件先经 ``POST {base}/file-urls/batch`` 申请预签名上传
    地址，上传时**不得携带 Authorization**（否则双重鉴权会被 403 拒绝）；随后有界轮询
    ``GET {base}/extract-results/batch/{batch_id}``，完成后从 ``full_zip_url`` 下载 ZIP
    并取其中的 ``full.md``。整个等待受 ``timeout_seconds`` 约束，绝不无限轮询。
    """

    provider_name = "mineru"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_version: str,
        timeout_seconds: float,
        max_bytes: int,
        poll_interval_seconds: float = MINERU_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_version = model_version
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.poll_interval_seconds = poll_interval_seconds

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_too_large")
        if not self.api_key:
            # 云 API 必须携带 Token；缺失时安全失败，绝不尝试匿名上传。
            raise ParserError("parser_unavailable")
        deadline = time.monotonic() + self.timeout_seconds
        batch_id, upload_url = self._request_upload(filename)
        self._upload(upload_url, data)
        zip_url = self._poll_result(batch_id, deadline)
        text = self._download_markdown(zip_url, deadline)
        normalized = normalize_text(text)
        if not normalized:
            raise ParserError("no_extractable_text")
        return ParsedDocument(
            text=normalized,
            title=filename,
            parser=self.provider_name,
            pages=1,
            truncated=False,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _request_upload(self, filename: str) -> tuple[str, str]:
        try:
            response = httpx.post(
                f"{self.base_url}/file-urls/batch",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"files": [{"name": filename}], "model_version": self.model_version},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise ParserError("parser_unavailable") from error
        data_block = body.get("data") or {}
        batch_id = data_block.get("batch_id")
        upload_urls = data_block.get("file_urls") or []
        if body.get("code") != 0 or not batch_id or not upload_urls:
            raise ParserError("parser_unavailable")
        return str(batch_id), str(upload_urls[0])

    def _upload(self, url: str, data: bytes) -> None:
        try:
            # 预签名地址自带鉴权；附加 Authorization 会被判定为双重鉴权并 403。
            response = httpx.put(url, content=data, timeout=self.timeout_seconds)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ParserError("parser_unavailable") from error

    def _poll_result(self, batch_id: str, deadline: float) -> str:
        while True:
            try:
                response = httpx.get(
                    f"{self.base_url}/extract-results/batch/{batch_id}",
                    headers=self._auth_headers(),
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, TypeError, ValueError) as error:
                raise ParserError("parser_unavailable") from error
            results = (body.get("data") or {}).get("extract_result") or []
            if results:
                state = str(results[0].get("state") or "")
                if state == "done":
                    zip_url = results[0].get("full_zip_url")
                    if not zip_url:
                        raise ParserError("parse_failed")
                    return str(zip_url)
                if state == "failed":
                    raise ParserError("parse_failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParserError("parser_unavailable")
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _download_markdown(self, zip_url: str, deadline: float) -> str:
        remaining = max(1.0, min(self.timeout_seconds, deadline - time.monotonic()))
        try:
            response = httpx.get(zip_url, timeout=remaining)
            response.raise_for_status()
            archive = zipfile.ZipFile(io.BytesIO(response.content))
        except (httpx.HTTPError, zipfile.BadZipFile, OSError) as error:
            raise ParserError("parser_unavailable") from error
        names = [name for name in archive.namelist() if name.casefold().endswith("full.md")]
        if not names:
            names = [name for name in archive.namelist() if name.casefold().endswith(".md")]
        if not names:
            raise ParserError("no_extractable_text")
        try:
            return archive.read(names[0]).decode("utf-8", errors="replace")
        except (KeyError, OSError) as error:
            raise ParserError("no_extractable_text") from error


def create_parser(settings: Settings) -> DocumentParser:
    if settings.parser_provider == "mineru":
        return MinerUParser(
            base_url=settings.mineru_base_url,
            api_key=settings.mineru_api_key.get_secret_value(),
            model_version=settings.mineru_model_version,
            timeout_seconds=settings.parser_timeout_seconds,
            max_bytes=settings.max_document_bytes,
        )
    return LightweightParser(max_bytes=settings.max_document_bytes)


def supported_content_type(
    content_type: str, filename: str, *, allow_remote_parser: bool = False
) -> bool:
    """上传类型门控：轻量解析器支持文本类；启用 MinerU 时额外接受其文档/图片类型。"""

    lowered = filename.casefold()
    if allow_remote_parser and lowered.endswith(MINERU_SUFFIXES):
        return True
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
    """把规范化文本切分为有界的 Parent 段落与带重叠的 Child 分块。"""

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
    # 超长单段落会被硬切，保证没有无界的 Parent chunk。
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
