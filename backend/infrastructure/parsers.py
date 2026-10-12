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
import logging
import re
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from backend.infrastructure.config import Settings
from backend.infrastructure.parser_errors import ParserError

logger = logging.getLogger(__name__)

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
#
# 本列表是**实测结论**而不是官方文档的照抄（ADR-037 / CR-INGEST batch 2 任务 4）：
# - 实测支持：`.pdf`、`.docx`、`.pptx`、`.xlsx`（最小 OOXML 样本真实调用 → `state=done`）；
# - 实测**不支持**：`.html/.htm` —— `model_version=pipeline` 在**提交阶段**直接拒绝
#   （`code=-10002`，provider 明确说明该模型版本无法处理 html），因此必须从 allowlist 移除，
#   让用户在提交前就得到明确拒绝，而不是上传后失败；
# - `.doc/.ppt`（旧二进制 Office）与 `.docx/.pptx` 同属 Office 家族、在 provider 文档列表内，
#   但本轮未单独取证（无法手工构造真实旧格式样本），保留在列表中；
# - `.xls`（BIFF8）**未获证明**：实测 provider 按内容而非扩展名路由，重命名样本不构成证据，
#   因此不加入 allowlist，保持提交前明确拒绝（runbook 已写明）；
# - 文本类（`.md/.txt/.csv/.json`）由本地 parser 处理，官方不支持，绝不送到 provider。
MINERU_SUFFIXES: tuple[str, ...] = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xlsx",
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
# 结构化块条目（provider 的 content_list.json）与块数量也各有上限（ADR-038 决策 1）：
# 它是可选增强，超预算只回退到文本分块，绝不让整次解析失败。
MAX_BLOCKS_ENTRY_BYTES = 64_000_000
MAX_BLOCKS = 20_000
# 分片 manifest 里持久化的结构化块（ADR-038）：有界，超限则不持久化并记录回退，
# 绝不写入无界内容。
MAX_BLOCKS_JSON_BYTES = 8_000_000


# ``ParserError`` 定义在 ``parser_errors`` 模块（供 pdf_slicing 共用，避免循环导入），
# 已在文件顶部导入，因此 ``from backend.infrastructure.parsers import ParserError`` 保持不变。


def normalize_text(text: str) -> str:
    """把解析结果规范化为可入库文本：去除控制字符、折叠空白与多余空行。"""

    without_controls = "".join(
        character if character.isprintable() or character in "\n\t" else " " for character in text
    )
    collapsed = re.sub(r"[ \t]+", " ", without_controls)
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


@dataclass(frozen=True)
class DocumentBlock:
    """provider 结果里的一个结构化块（ADR-038 决策 1）。

    ``page_index`` 是**相对本次提交文件**的 0-based 页号（provider 的 ``page_idx``）；
    摄取层按分片页范围把它换算成绝对页码，这里绝不猜测页码。

    ``block_type`` 归一为 ``paragraph | heading | table | code | image | list``；
    ``heading_level`` 仅对标题块有值（来自 ``text_level``）。
    """

    text: str
    block_type: str
    page_index: int | None
    heading_level: int | None
    order: int


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    title: str
    parser: str
    pages: int
    truncated: bool
    warnings: tuple[str, ...] = ()
    # provider 提供的结构化块；为空表示只能走文本分块（ADR-038 决策 1 的回退）。
    blocks: tuple[DocumentBlock, ...] = ()


@dataclass(frozen=True)
class ProviderTask:
    """一次**已提交**的远端解析任务（ADR-037）。

    ``batch_id`` 是恢复的唯一凭据：轮询超时**不等于**任务未创建，因此调用方必须在
    ``submit`` 之后立刻把它持久化，超时/重试时先按同一个 batch id 续轮询，
    绝不重复提交同一份字节（重复提交会重复消耗 provider 额度）。
    """

    batch_id: str


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
            # 纯文本没有可靠页码概念：标记为未知（0），绝不伪造为第 1 页（ADR-037）。
            pages=0,
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
        max_blocks_entry_bytes: int = MAX_BLOCKS_ENTRY_BYTES,
        max_blocks: int = MAX_BLOCKS,
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
        self.max_blocks_entry_bytes = max_blocks_entry_bytes
        self.max_blocks = max_blocks

    def provider_for(self, *, filename: str, content_type: str) -> str:
        return self.provider_name

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        """单次调用的便捷封装：``submit`` + ``fetch``（不改变既有调用方语义）。"""

        task = self.submit(filename=filename, content_type=content_type, data=data)
        return self.fetch(task.batch_id, title=filename)

    def submit(self, *, filename: str, content_type: str, data: bytes) -> ProviderTask:
        """申请上传地址 → 上传字节 → 返回 provider batch id。

        这一步是**副作用**（消耗额度、创建远端任务）：因此只在本地边界检查通过后执行，
        且返回值必须由调用方立刻持久化——轮询超时并不代表任务未创建（ADR-037）。
        """

        if not data:
            raise ParserError("empty_document")
        if len(data) > self.max_bytes:
            raise ParserError("document_exceeds_parser_limit")
        if not self.api_key:
            # 云 API 必须携带 Token；缺失时安全失败，绝不尝试匿名上传。
            raise ParserError("parser_unavailable")
        batch_id, upload_url = self._request_upload(filename)
        self._upload(upload_url, data)
        return ProviderTask(batch_id=batch_id)

    def fetch(
        self, batch_id: str, *, deadline: float | None = None, title: str = ""
    ) -> ParsedDocument:
        """轮询同一个 batch id 直到完成并取回文本；等待受 ``deadline`` 约束。

        ``deadline`` 是 ``time.monotonic()`` 的绝对时刻；未给出时按 ``timeout_seconds``
        从现在起算。超时抛 ``parse_timeout``（与「提交失败」区分），调用方据此保留
        batch id 并在重试时续轮询。
        """

        if not self.api_key:
            raise ParserError("parser_unavailable")
        if deadline is None:
            deadline = time.monotonic() + self.timeout_seconds
        zip_url = self._poll_result(batch_id, deadline)
        text, blocks, warnings = self._download_archive(zip_url, deadline)
        normalized = normalize_text(text)
        if not normalized:
            raise ParserError("no_extractable_text")
        return ParsedDocument(
            text=normalized,
            title=title,
            parser=self.provider_name,
            # provider 结果不含可靠页数：标记未知（0），真实页数由摄取层按分片页范围给出。
            pages=0,
            truncated=False,
            warnings=warnings,
            blocks=blocks,
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
                    # 只记录「类型与形状」，绝不把 provider 原文写进日志或错误码；
                    # 失败定位由 manifest 的分片页范围提供（ADR-037）。
                    logger.info(
                        "mineru_task_failed state=%s has_message=%s",
                        state,
                        bool(results[0].get("err_msg")),
                    )
                    raise ParserError("parse_failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 超时 ≠ 任务未创建：调用方必须保留 batch id 并在重试时续轮询。
                raise ParserError("parse_timeout")
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _download_archive(
        self, zip_url: str, deadline: float
    ) -> tuple[str, tuple[DocumentBlock, ...], tuple[str, ...]]:
        """有界下载结果归档并读取 Markdown 与结构化块（绝不写磁盘、绝不信任归档内路径）。"""

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
        return self._read_archive(bytes(payload))

    def _read_archive(
        self, payload: bytes
    ) -> tuple[str, tuple[DocumentBlock, ...], tuple[str, ...]]:
        """解压前校验条目数/解压总量/路径，读取时再按上限有界截断读取。"""

        warnings: list[str] = []
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
            blocks, block_warnings = self._read_blocks(archive, infos)
        warnings.extend(block_warnings)
        return raw.decode("utf-8", errors="replace"), blocks, tuple(warnings)

    def _read_blocks(
        self, archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]
    ) -> tuple[tuple[DocumentBlock, ...], tuple[str, ...]]:
        """读取 provider 的 ``content_list.json`` 并归一为有界结构化块。

        结构化块是**可选增强**（ADR-038 决策 1）：条目缺失、超预算或格式不符时返回空元组
        并给出一个安全警告码，让摄取层回退到文本分块，**绝不编造结构、也绝不让整次解析失败**。
        """

        entry = next(
            (
                info
                for info in infos
                if not info.is_dir() and info.filename.casefold().endswith("content_list.json")
            ),
            None,
        )
        if entry is None:
            return (), ("structure_unavailable",)
        if self.max_blocks_entry_bytes and entry.file_size > self.max_blocks_entry_bytes:
            logger.info("mineru_content_list_over_budget")
            return (), ("structure_over_budget",)
        try:
            with archive.open(entry) as handle:
                raw = handle.read(self.max_blocks_entry_bytes + 1)
        except (KeyError, OSError, zipfile.BadZipFile):
            return (), ("structure_unreadable",)
        if len(raw) > self.max_blocks_entry_bytes:
            return (), ("structure_over_budget",)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return (), ("structure_unreadable",)
        if not isinstance(payload, list):
            return (), ("structure_unreadable",)
        blocks = _normalize_blocks(payload, limit=self.max_blocks)
        if not blocks:
            return (), ("structure_empty",)
        if len(payload) > self.max_blocks:
            return blocks, ("structure_truncated",)
        return blocks, ()


# provider 的块类型 → 项目归一化块类型（ADR-038 决策 3）。未知类型按段落处理，
# 绝不丢弃内容；标题由 ``text_level`` 判定，不依赖 provider 的类型命名。
_BLOCK_TYPES: dict[str, str] = {
    "text": "paragraph",
    "paragraph": "paragraph",
    "title": "heading",
    "heading": "heading",
    "table": "table",
    "code": "code",
    "image": "image",
    "figure": "image",
    "equation": "equation",
    "interline_equation": "equation",
    "list": "list",
}


def _as_text_lines(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _block_text(raw: dict) -> str:
    """把 provider 块归一为可索引文本：正文 + 标题/图注 + 脚注，顺序稳定。"""

    body = ""
    for key in ("text", "table_body", "code_body"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            body = value.strip()
            break
    parts: list[str] = []
    for key in ("table_caption", "code_caption", "image_caption"):
        parts.extend(_as_text_lines(raw.get(key)))
    if body:
        parts.append(body)
    for key in ("table_footnote", "code_footnote", "image_footnote"):
        parts.extend(_as_text_lines(raw.get(key)))
    return "\n".join(part for part in parts if part).strip()


def _normalize_blocks(payload: list, *, limit: int) -> tuple[DocumentBlock, ...]:
    """把 provider 的 ``content_list.json`` 归一为有界 ``DocumentBlock`` 序列。

    无正文且无标题/图注的块（例如没有 caption 的图片）不产生可索引文本，直接跳过；
    没有可靠 ``page_idx`` 的块把页码记为 ``None``，**绝不伪造页码**。
    """

    blocks: list[DocumentBlock] = []
    for order, raw in enumerate(payload):
        if len(blocks) >= limit:
            break
        if not isinstance(raw, dict):
            continue
        text = _block_text(raw)
        if not text:
            continue
        level = raw.get("text_level")
        heading_level = int(level) if isinstance(level, int) and level > 0 else None
        raw_type = str(raw.get("type") or "").strip().casefold()
        if heading_level is not None and raw_type in {"text", "title", "heading"}:
            block_type = "heading"
        else:
            block_type = _BLOCK_TYPES.get(raw_type, "paragraph")
        page = raw.get("page_idx")
        page_index = int(page) if isinstance(page, int) and page >= 0 else None
        blocks.append(
            DocumentBlock(
                text=text,
                block_type=block_type,
                page_index=page_index,
                heading_level=heading_level,
                order=order,
            )
        )
    return tuple(blocks)


def serialize_blocks(blocks: Sequence[DocumentBlock]) -> str | None:
    """把结构化块序列化为紧凑 JSON；超过上限时返回 ``None``（调用方记录回退）。

    复用分片时必须能取回同样的结构，否则「首次结构化、重建索引回退文本」会造成
    同一文档两次索引行为不一致（ADR-038 决策 1/6）。
    """

    if not blocks:
        return None
    payload = json.dumps(
        [
            [block.text, block.block_type, block.page_index, block.heading_level, block.order]
            for block in blocks
        ],
        ensure_ascii=False,
    )
    if len(payload.encode("utf-8")) > MAX_BLOCKS_JSON_BYTES:
        return None
    return payload


def deserialize_blocks(payload: str | None) -> tuple[DocumentBlock, ...]:
    """从 manifest 取回结构化块；任何异常都退化为空元组（回退文本分块），绝不抛出。"""

    if not payload:
        return ()
    try:
        raw = json.loads(payload)
    except (ValueError, TypeError):
        return ()
    if not isinstance(raw, list):
        return ()
    blocks: list[DocumentBlock] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 5:
            return ()
        text, block_type, page_index, heading_level, order = item
        if not isinstance(text, str) or not text:
            return ()
        blocks.append(
            DocumentBlock(
                text=text,
                block_type=str(block_type or "paragraph"),
                page_index=int(page_index) if isinstance(page_index, int) else None,
                heading_level=int(heading_level) if isinstance(heading_level, int) else None,
                order=int(order) if isinstance(order, int) else 0,
            )
        )
    return tuple(blocks)


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
    """上传类型门控：文本类始终支持；远端类型仅当该档位真的能解析时才接受。

    HTML 是**明确不支持**的格式（ADR-037 实测）：`model_version=pipeline` 在提交阶段
    直接拒绝 html，本地也没有结构化 HTML 提取。因此这里在**提交前**拒绝，而不是先收下
    字节、上传后再失败，也绝不把带标签的源码当正文索引。
    """

    if content_type == "text/html" or filename.casefold().endswith((".html", ".htm")):
        return False
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
    # 结构元数据（ADR-038）：文本回退路径全部为 None/默认值，绝不伪造。
    section_path: str | None = None
    block_type: str = "paragraph"
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0


@dataclass(frozen=True)
class ChildChunk:
    ordinal: int
    parent_ordinal: int
    content: str
    section_path: str | None = None
    block_type: str = "paragraph"
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0


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
