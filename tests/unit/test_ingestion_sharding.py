"""MinerU 分片编排（ADR-037）的离线单测。

用真实 PDF 字节 + 记录型 provider 替身驱动 ``IngestionService`` 的分片路径，覆盖：

- 313 页样本切成 1–200 / 201–313；
- 复用键命中时不调用 provider（成功分片不重复付费解析）；
- 轮询超时保留 batch id，重试续轮询同一个 provider 任务（不重复提交）；
- 单片失败后只重试该片，成功片继续复用；
- 分片闭合校验：缺片 / 未完成 / 页范围断裂都不得通过。

不触网、不连库、不消耗 MinerU 额度。
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
from datetime import datetime

import pytest
from pypdf import PdfWriter

from backend.application.ingestion import IngestionService, _IngestionFailure
from backend.infrastructure.config import Settings
from backend.infrastructure.parser_errors import ParserError
from backend.infrastructure.parsers import MinerUParser, ParsedDocument, ProviderTask
from backend.infrastructure.repositories.documents import (
    SHARD_STATUS_FAILED,
    SHARD_STATUS_PENDING,
    SHARD_STATUS_RUNNING,
    SHARD_STATUS_SUCCESS,
    IngestionJobRecord,
    IngestionShardRecord,
)

NOW = datetime(2026, 10, 9, 12, 0, 0)


def _blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _job(index_version: str, *, document_id: str = "doc-1") -> IngestionJobRecord:
    return IngestionJobRecord(
        ingestion_job_id="job-1",
        document_id=document_id,
        index_version=index_version,
        job_type="ingest",
        status="running",
        attempt=1,
        failure_code=None,
        failure_message=None,
        claimed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        finished_at=None,
    )


class _FakeShardRepository:
    """与 ``IngestionShardRepository`` 同语义的内存替身（状态机一致）。"""

    def __init__(self) -> None:
        self.rows: dict[str, IngestionShardRecord] = {}
        self._sequence = 0

    def _next_id(self) -> str:
        self._sequence += 1
        return f"shard-{self._sequence}"

    def create(self, **fields) -> IngestionShardRecord:
        record = IngestionShardRecord(
            shard_id=self._next_id(),
            ingestion_job_id=fields["ingestion_job_id"],
            document_id=fields["document_id"],
            index_version=fields["index_version"],
            shard_index=fields["shard_index"],
            page_start=fields.get("page_start"),
            page_end=fields.get("page_end"),
            byte_size=fields["byte_size"],
            sha256=fields["sha256"],
            parser_provider=fields["parser_provider"],
            parser_options=fields["parser_options"],
            provider_batch_id=fields.get("provider_batch_id"),
            status=SHARD_STATUS_PENDING,
            reused=False,
            attempt=0,
            char_count=0,
            failure_code=None,
            created_at=NOW,
            updated_at=NOW,
            finished_at=None,
            blocks_json=fields.get("blocks_json"),
        )
        self.rows[record.shard_id] = record
        return record

    def get_by_index(
        self, *, index_version: str, shard_index: int, with_text: bool = False
    ) -> IngestionShardRecord | None:
        for record in self.rows.values():
            if record.index_version == index_version and record.shard_index == shard_index:
                return record
        return None

    def claim(self, shard_id: str) -> bool:
        record = self.rows[shard_id]
        if record.status != SHARD_STATUS_PENDING:
            return False
        self.rows[shard_id] = dataclasses.replace(
            record, status=SHARD_STATUS_RUNNING, attempt=record.attempt + 1
        )
        return True

    def set_provider_batch_id(self, shard_id: str, *, provider_batch_id: str) -> bool:
        record = self.rows[shard_id]
        self.rows[shard_id] = dataclasses.replace(record, provider_batch_id=provider_batch_id)
        return True

    def mark_success(
        self,
        shard_id: str,
        *,
        extracted_text: str,
        reused: bool = False,
        blocks_json: str | None = None,
    ) -> bool:
        record = self.rows[shard_id]
        if record.status == SHARD_STATUS_SUCCESS:
            return False
        self.rows[shard_id] = dataclasses.replace(
            record,
            status=SHARD_STATUS_SUCCESS,
            extracted_text=extracted_text,
            char_count=len(extracted_text),
            reused=reused,
            blocks_json=blocks_json,
            failure_code=None,
            finished_at=NOW,
        )
        return True

    def mark_failed(self, shard_id: str, *, failure_code: str) -> bool:
        record = self.rows[shard_id]
        if record.status == SHARD_STATUS_SUCCESS:
            return False
        self.rows[shard_id] = dataclasses.replace(
            record, status=SHARD_STATUS_FAILED, failure_code=failure_code, finished_at=NOW
        )
        return True

    def find_reusable(
        self, *, sha256: str, parser_provider: str, parser_options: str
    ) -> IngestionShardRecord | None:
        candidates = [
            record
            for record in self.rows.values()
            if record.sha256 == sha256
            and record.parser_provider == parser_provider
            and record.parser_options == parser_options
            and record.status == SHARD_STATUS_SUCCESS
            and record.extracted_text is not None
        ]
        return candidates[-1] if candidates else None

    def find_resumable(
        self, *, sha256: str, parser_provider: str, parser_options: str
    ) -> IngestionShardRecord | None:
        candidates = [
            record
            for record in self.rows.values()
            if record.sha256 == sha256
            and record.parser_provider == parser_provider
            and record.parser_options == parser_options
            and record.status == SHARD_STATUS_RUNNING
            and record.provider_batch_id is not None
        ]
        return candidates[-1] if candidates else None

    def list_for_version(self, index_version: str, *, with_text: bool = False):
        return sorted(
            (record for record in self.rows.values() if record.index_version == index_version),
            key=lambda record: record.shard_index,
        )


class _FakeContents:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    def upsert(
        self, *, document_id: str, extracted_text: str, parser_provider: str, truncated: bool
    ):
        self.stored[document_id] = extracted_text

    def get(self, document_id: str) -> str | None:
        return self.stored.get(document_id)


class _FakeMinerU(MinerUParser):
    """记录调用的 MinerU 替身：可注入超时与失败，验证复用/恢复/单侧重试。"""

    def __init__(
        self, *, timeout_batches: set[str] | None = None, fail_batches: set[str] | None = None
    ):
        super().__init__(
            base_url="https://mineru.example/api/v4",
            api_key="test-token",
            model_version="pipeline",
            timeout_seconds=5.0,
            max_bytes=200_000_000,
            poll_interval_seconds=0.01,
        )
        self.submitted: list[str] = []
        self.fetched: list[str] = []
        self._timeout_once = set(timeout_batches or set())
        self._fail = set(fail_batches or set())

    def submit(self, *, filename: str, content_type: str, data: bytes) -> ProviderTask:
        self.submitted.append(filename)
        return ProviderTask(batch_id=f"batch-{len(self.submitted)}")

    def fetch(
        self, batch_id: str, *, deadline: float | None = None, title: str = ""
    ) -> ParsedDocument:
        self.fetched.append(batch_id)
        if batch_id in self._timeout_once:
            self._timeout_once.discard(batch_id)
            raise ParserError("parse_timeout")
        if batch_id in self._fail:
            raise ParserError("parse_failed")
        return ParsedDocument(
            text=f"<{title}>", title=title, parser=self.provider_name, pages=0, truncated=False
        )


def _service(*, shards: _FakeShardRepository, remote: _FakeMinerU, max_pages: int = 200):
    service = object.__new__(IngestionService)
    service.settings = Settings(
        _env_file=None, mineru_max_pages=max_pages, parser_timeout_seconds=5.0
    )
    service.shards = shards
    service.parser = remote
    service.contents = _FakeContents()
    return service


def _extract(service: IngestionService, job: IngestionJobRecord, data: bytes):
    return service._extract_text_sharded(
        job, data, filename="book.pdf", remote=service.parser, owner_token="owner"
    )


# --- 分片规划与执行 --------------------------------------------------------


def test_the_313_page_book_is_split_into_1_200_and_201_313():
    shards = _FakeShardRepository()
    remote = _FakeMinerU()
    service = _service(shards=shards, remote=remote)

    extraction = _extract(service, _job("v1"), _blank_pdf(313))

    assert extraction.shard_count == 2
    assert extraction.page_count == 313
    assert len(remote.submitted) == 2
    assert "p1-200" in remote.submitted[0]
    assert "p201-313" in remote.submitted[1]
    ranges = [(row.page_start, row.page_end) for row in shards.list_for_version("v1")]
    assert ranges == [(1, 200), (201, 313)]
    assert all(row.status == SHARD_STATUS_SUCCESS for row in shards.list_for_version("v1"))


def test_a_second_upload_of_the_same_bytes_does_not_call_the_provider_again():
    shards = _FakeShardRepository()
    remote = _FakeMinerU()
    service = _service(shards=shards, remote=remote)
    data = _blank_pdf(313)

    first = _extract(service, _job("v1"), data)
    submits_after_first = len(remote.submitted)
    second = _extract(service, _job("v2"), data)

    # 复用键命中：没有新的 provider 调用，文本完全相同。
    assert len(remote.submitted) == submits_after_first
    assert second.text == first.text
    reused_rows = [row for row in shards.list_for_version("v2")]
    assert all(row.reused for row in reused_rows)
    assert all(row.status == SHARD_STATUS_SUCCESS for row in reused_rows)


def test_a_timed_out_shard_is_resumed_on_the_same_provider_task():
    shards = _FakeShardRepository()
    remote = _FakeMinerU(timeout_batches={"batch-1"})
    service = _service(shards=shards, remote=remote)
    data = _blank_pdf(313)

    with pytest.raises(_IngestionFailure) as failure:
        _extract(service, _job("v1"), data)
    assert failure.value.code == "parse_timeout"
    # 超时 ≠ 未创建：分片保留 running + batch id，供重试续轮询。
    timed_out = shards.list_for_version("v1")[0]
    assert timed_out.status == SHARD_STATUS_RUNNING
    assert timed_out.provider_batch_id == "batch-1"

    submits_before_retry = len(remote.submitted)
    extraction = _extract(service, _job("v2"), data)

    assert extraction.shard_count == 2
    # 第一片续轮询同一个 batch id（没有第二次提交）；第二片是新的提交。
    assert remote.fetched.count("batch-1") == 2
    assert len(remote.submitted) == submits_before_retry + 1


def test_a_failed_shard_is_retried_alone_while_successful_shards_are_reused():
    shards = _FakeShardRepository()
    remote = _FakeMinerU(fail_batches={"batch-2"})
    service = _service(shards=shards, remote=remote)
    data = _blank_pdf(313)

    with pytest.raises(_IngestionFailure) as failure:
        _extract(service, _job("v1"), data)
    assert failure.value.code == "parse_failed"
    first_version = {row.shard_index: row.status for row in shards.list_for_version("v1")}
    assert first_version == {0: SHARD_STATUS_SUCCESS, 1: SHARD_STATUS_FAILED}

    submits_before_retry = len(remote.submitted)
    extraction = _extract(service, _job("v2"), data)

    assert extraction.shard_count == 2
    # 只有失败的第二片重新提交；成功的第一片复用。
    assert len(remote.submitted) == submits_before_retry + 1
    retried = [row for row in shards.list_for_version("v2")]
    assert retried[0].reused is True
    assert retried[1].reused is False
    assert all(row.status == SHARD_STATUS_SUCCESS for row in retried)


def test_an_existing_unfinished_shard_in_the_same_version_resumes_in_place():
    # 同一 index version 内已有一行未完成分片（例如同一执行内的再次尝试）：
    # 不新建行（UNIQUE(index_version, shard_index)），而是原地续轮询它的 batch id。
    shards = _FakeShardRepository()
    remote = _FakeMinerU()
    service = _service(shards=shards, remote=remote)
    data = _blank_pdf(5)
    digest = hashlib.sha256(data).hexdigest()

    seeded = shards.create(
        ingestion_job_id="job-1",
        document_id="doc-1",
        index_version="v1",
        shard_index=0,
        byte_size=len(data),
        sha256=digest,
        parser_provider="mineru",
        parser_options="pipeline",
        page_start=1,
        page_end=5,
        provider_batch_id="batch-existing",
    )
    shards.claim(seeded.shard_id)

    extraction = _extract(service, _job("v1"), data)

    assert extraction.shard_count == 1
    assert remote.submitted == []
    assert remote.fetched == ["batch-existing"]
    assert len(shards.list_for_version("v1")) == 1


def test_non_pdf_remote_documents_get_a_single_shard_without_page_range():
    shards = _FakeShardRepository()
    remote = _FakeMinerU()
    service = _service(shards=shards, remote=remote)

    extraction = service._extract_text_sharded(
        _job("v1"), b"PK\x03\x04fake-docx", filename="report.docx", remote=remote, owner_token="o"
    )

    assert extraction.shard_count == 1
    assert extraction.page_count == 0
    row = shards.list_for_version("v1")[0]
    assert row.page_start is None and row.page_end is None
    assert row.status == SHARD_STATUS_SUCCESS


# --- 分片闭合校验 ----------------------------------------------------------


def _seed(shards: _FakeShardRepository, *, page_start, page_end, status) -> None:
    record = shards.create(
        ingestion_job_id="job-1",
        document_id="doc-1",
        index_version="v1",
        shard_index=len(shards.list_for_version("v1")),
        byte_size=10,
        sha256=hashlib.sha256(str(page_start).encode()).hexdigest(),
        parser_provider="mineru",
        parser_options="pipeline",
        page_start=page_start,
        page_end=page_end,
    )
    shards.claim(record.shard_id)
    if status == SHARD_STATUS_SUCCESS:
        shards.mark_success(record.shard_id, extracted_text="text")
    elif status == SHARD_STATUS_FAILED:
        shards.mark_failed(record.shard_id, failure_code="parse_failed")


def _verify(shards: _FakeShardRepository, *, expected_shards: int, expected_pages: int) -> None:
    service = _service(shards=shards, remote=_FakeMinerU())
    service._verify_shards(
        index_version="v1", expected_shard_count=expected_shards, expected_pages=expected_pages
    )


def test_closure_passes_for_a_complete_manifest():
    shards = _FakeShardRepository()
    _seed(shards, page_start=1, page_end=200, status=SHARD_STATUS_SUCCESS)
    _seed(shards, page_start=201, page_end=313, status=SHARD_STATUS_SUCCESS)
    _verify(shards, expected_shards=2, expected_pages=313)


def test_closure_rejects_a_missing_shard():
    shards = _FakeShardRepository()
    _seed(shards, page_start=1, page_end=200, status=SHARD_STATUS_SUCCESS)
    with pytest.raises(_IngestionFailure) as failure:
        _verify(shards, expected_shards=2, expected_pages=313)
    assert failure.value.code == "shard_manifest_mismatch"


def test_closure_rejects_an_unfinished_shard():
    shards = _FakeShardRepository()
    _seed(shards, page_start=1, page_end=200, status=SHARD_STATUS_SUCCESS)
    _seed(shards, page_start=201, page_end=313, status=SHARD_STATUS_RUNNING)
    with pytest.raises(_IngestionFailure) as failure:
        _verify(shards, expected_shards=2, expected_pages=313)
    assert failure.value.code == "shard_incomplete"


def test_closure_rejects_a_page_range_gap():
    shards = _FakeShardRepository()
    _seed(shards, page_start=1, page_end=200, status=SHARD_STATUS_SUCCESS)
    _seed(shards, page_start=250, page_end=313, status=SHARD_STATUS_SUCCESS)
    with pytest.raises(_IngestionFailure) as failure:
        _verify(shards, expected_shards=2, expected_pages=313)
    assert failure.value.code == "shard_range_gap"


def test_closure_rejects_an_incomplete_page_cover():
    shards = _FakeShardRepository()
    _seed(shards, page_start=1, page_end=200, status=SHARD_STATUS_SUCCESS)
    _seed(shards, page_start=201, page_end=300, status=SHARD_STATUS_SUCCESS)
    with pytest.raises(_IngestionFailure) as failure:
        _verify(shards, expected_shards=2, expected_pages=313)
    assert failure.value.code == "shard_range_gap"
