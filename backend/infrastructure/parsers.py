"""Parser Adapter 边界（ADR-007 / ADR-036）与 Parent/Child 分块。

``MinerUParser`` 对接 mineru.net v4 云解析 API（异步任务：预签名上传 → 轮询结果 →
下载 ZIP 取 full.md）；``LightweightParser`` 解析纯文本、Markdown、CSV 与 JSON；
``RoutedParser`` 按文件类型路由：文本类始终走本地解析（MinerU 档位下同样如此），
其余类型走远端 provider。parser 返回规范化文本与安全元数据，且绝不触碰 Graph State。

文本长度是**显式预算**而不是静默截断（ADR-036 决策 2）：超出预算时抛
``extracted_text_too_large``，由摄取层明确失败，绝不索引一个前缀冒充全文。
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

# MinerU 精准解析 API 支持的上传类型（官方文档 v4：单份 ≤200,000,000 字节且 ≤200 页）。
# 注意：官方不支持 .md/.txt/.csv/.json 等文本类，因此这些类型由本地 parser 处理；
# .xls/.xlsx 的 provider 能力尚未核验（P2 剩余工作），在核验前不接受，绝不只改扩展名
# allowlist（ADR-036 决策 8）。
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

# 结果归档的强制边界（ADR-036 决策 6）：下载字节、条目数、解压总量与单个 Markdown
# 条目都有界，路径一律校验，绝不写磁盘。
MAX_ARCHIVE_BYTES = 400_000_000
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_UNCOMPRESSED_BYTES = 400_000_000
MAX_MARKDOWN_ENTRY_BYTES = 64_000_000


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

    def provider_for(self, *, filename: str, content_type: str) -> str: ...

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument: ...


def is_text_document(content_type: str, filename: str) -> bool:
    """文本类文档（本地 parser 可完整处理，永不需要远端 provider）。"""

    lowered = filename.casefold()
    return (
        content_type in TEXT_CONTENT_TYPES
        or content_type.startswith("text/")
        or lowered.endswith(SUPPORTED_SUFFIXES)
    )


class LightweightParser:
    """本地 parser：纯文本、Markdown、CSV 与 JSON（有界、无静默截断）。"""

    provider_name = "lightweight"

    def __init__(self, *, max_bytes: int, max_chars: int = 4_000_000) -> None:
        self.max_bytes = max_bytes
        self.max_chars = max_chars

    def provider_for(self, *, filename: str, content_type: str) -> str:
        return self.provider_name

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_too_large")
        text = self._decode(data, content_type, filename)
        normalized = normalize_text(text)
        if not normalized.strip():
            raise ParserError("no_extractable_text")
        if len(normalized) > self.max_chars:
            # 显式预算：超出即失败，绝不索引前缀冒充全文（ADR-036 决策 2）。
            raise ParserError("extracted_text_too_large")
        return ParsedDocument(
            text=normalized,
            title=filename,
            parser=self.provider_name,
            pages=1,
            truncated=False,
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

    ``max_bytes`` 必须是**远端单份硬限**（``MINERU_MAX_BYTES``），而不是本地接受上限：
    必然被 provider 拒绝的文件在本地就明确失败（ADR-036 决策 1）。
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
        max_archive_bytes: int = MAX_ARCHIVE_BYTES,
        max_zip_entries: int = MAX_ZIP_ENTRIES,
        max_zip_uncompressed_bytes: int = MAX_ZIP_UNCOMPRESSED_BYTES,
        max_markdown_entry_bytes: int = MAX_MARKDOWN_ENTRY_BYTES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_version = model_version
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.poll_interval_seconds = poll_interval_seconds
        self.max_archive_bytes = max_archive_bytes
        self.max_zip_entries = max_zip_entries
        self.max_zip_uncompressed_bytes = max_zip_uncompressed_bytes
        self.max_markdown_entry_bytes = max_markdown_entry_bytes

    def provider_for(self, *, filename: str, content_type: str) -> str:
        return self.provider_name

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_exceeds_parser_limit")
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
        """有界下载结果归档并读取 Markdown 条目（绝不写磁盘、绝不信任归档内路径）。"""

        if not _is_allowed_result_url(zip_url):
            raise ParserError("parser_result_unsafe")
        remaining = max(1.0, min(self.timeout_seconds, deadline - time.monotonic()))
        try:
            with httpx.stream("GET", zip_url, timeout=remaining, follow_redirects=True) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_archive_bytes:
                    raise ParserError("parser_result_too_large")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > self.max_archive_bytes:
                        # 声明长度不可信：边读边限，绝不把无界内容读进内存。
                        raise ParserError("parser_result_too_large")
        except ParserError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise ParserError("parser_unavailable") from error
        return self._read_markdown_from_archive(bytes(payload))

    def _read_markdown_from_archive(self, payload: bytes) -> str:
        """解压前校验条目数/解压总量/路径，读取时再按上限有界截断读取。"""

        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except (zipfile.BadZipFile, OSError) as error:
            raise ParserError("parser_unavailable") from error
        with archive:
            infos = archive.infolist()
            if len(infos) > self.max_zip_entries:
                raise ParserError("parser_result_too_large")
            total_uncompressed = 0
            candidates: list[zipfile.ZipInfo] = []
            for info in infos:
                if not _is_safe_archive_name(info.filename):
                    # 目录越界或可疑路径：整个结果视为不可信，明确失败。
                    raise ParserError("parser_result_unsafe")
                total_uncompressed += max(0, int(info.file_size))
                if total_uncompressed > self.max_zip_uncompressed_bytes:
                    raise ParserError("parser_result_too_large")
                lowered = info.filename.casefold()
                if not info.is_dir() and lowered.endswith(".md"):
                    candidates.append(info)
            if not candidates:
                raise ParserError("no_extractable_text")
            # 优先使用 provider 的合并结果 full.md，否则取第一个 Markdown 条目。
            candidates.sort(key=lambda info: (not info.filename.casefold().endswith("full.md"),))
            chosen = candidates[0]
            if self.max_markdown_entry_bytes and chosen.file_size > self.max_markdown_entry_bytes:
                raise ParserError("parser_result_too_large")
            try:
                with archive.open(chosen) as handle:
                    raw = handle.read(self.max_markdown_entry_bytes + 1)
            except (KeyError, OSError, zipfile.BadZipFile) as error:
                raise ParserError("no_extractable_text") from error
            if len(raw) > self.max_markdown_entry_bytes:
                raise ParserError("parser_result_too_large")
        return raw.decode("utf-8", errors="replace")


def _is_safe_archive_name(name: str) -> bool:
    """归档条目名必须相对、无越界（绝不写磁盘，但仍拒绝可疑路径）。"""

    if not name or name.startswith(("/", "\\")):
        return False
    if "\\" in name or ":" in name:
        return False
    parts = [part for part in name.split("/") if part not in ("", ".")]
    return all(part != ".." for part in parts)


def _is_allowed_result_url(url: str) -> bool:
    """结果下载地址必须来自 provider 响应里的 http(s) 地址（拒绝其它 scheme）。"""

    return url.startswith("https://") or url.startswith("http://")


class RoutedParser:
    """按文件类型路由的 parser（ADR-036 决策 5）。

    文本类文件（``text/*``、``.txt/.md/.markdown/.csv/.json``）永远走本地 parser：
    MinerU v4 云 API 不支持这些类型（实测返回 ``unsupported file type``），此前在
    ``PARSER_PROVIDER=mineru`` 档位下上传 Markdown 必然失败。其余类型走远端 provider。
    ``provider_for`` 报告**真正解析该文件**的 provider，版本元数据据此记录。
    """

    provider_name = "routed"

    def __init__(self, *, local: LightweightParser, remote: MinerUParser) -> None:
        self.local = local
        self.remote = remote

    def provider_for(self, *, filename: str, content_type: str) -> str:
        return self.parser_for(filename=filename, content_type=content_type).provider_name

    def parser_for(self, *, filename: str, content_type: str) -> DocumentParser:
        if is_text_document(content_type, filename):
            return self.local
        return self.remote

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        return self.parser_for(filename=filename, content_type=content_type).parse(
            filename=filename, content_type=content_type, data=data
        )


def create_parser(settings: Settings) -> DocumentParser:
    """按配置装配 parser；MinerU 档位下文本类仍由本地 parser 处理（ADR-036 决策 5）。"""

    local = LightweightParser(
        max_bytes=settings.max_document_bytes, max_chars=settings.max_extracted_chars
    )
    if settings.parser_provider != "mineru":
        return local
    remote = MinerUParser(
        base_url=settings.mineru_base_url,
        api_key=settings.mineru_api_key.get_secret_value(),
        model_version=settings.mineru_model_version,
        timeout_seconds=settings.parser_timeout_seconds,
        # 远端单份硬限（不是本地接受上限）：必然被 provider 拒绝的文件在本地就失败。
        max_bytes=settings.mineru_max_bytes,
    )
    return RoutedParser(local=local, remote=remote)


def supported_content_type(
    content_type: str, filename: str, *, allow_remote_parser: bool = False
) -> bool:
    """上传类型门控：文本类始终支持；远端类型仅当该档位真的能解析时才接受。"""

    if is_text_document(content_type, filename):
        return True
    if allow_remote_parser and filename.casefold().endswith(MINERU_SUFFIXES):
        return True
    return False


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
