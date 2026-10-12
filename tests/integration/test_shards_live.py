"""分片 manifest 与 MinerU 分片编排的实时集成检查（ADR-037）。

在真实 MySQL + Milvus 上跑完整摄取链路（manifest → 分片 → 合并 → 分块 → 向量 → promote），
provider 用记录型替身，因此不消耗 MinerU 额度、也不依赖外网。

覆盖验收项：313 页按 1–200 / 201–313 分片、成功分片不重复付费解析、失败片可恢复、
缺片/失败不得 promote、真实页码可回查（manifest 的页范围是权威页码来源）。
"""

from __future__ import annotations

import io
import re
import uuid

import pytest
from pypdf import PdfWriter

from backend.application.index_registry import RegistryParentContext
from backend.infrastructure.parser_errors import ParserError
from backend.infrastructure.parsers import (
    DocumentBlock,
    LightweightParser,
    MinerUParser,
    ParsedDocument,
    ProviderTask,
    RoutedParser,
    normalize_text,
)


def _blank_pdf(pages: int, *, seed: str) -> bytes:
    """构造页数确定、字节唯一的 PDF。

    唯一性必须体现在**页面对象**上：``slice_pages`` 只复制页，不复制文档 Info，
    所以仅靠 metadata 去重会让不同用例切出完全相同的分片字节（进而命中复用键）。
    """

    offset = int(uuid.uuid5(uuid.NAMESPACE_OID, seed).hex[:4], 16) % 97
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200 + offset, height=200 + offset)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _RecordingMinerU(MinerUParser):
    """记录型 MinerU 替身：可按分片序号注入失败/超时，并统计真实"提交"次数。"""

    def __init__(self, *, fail_batch_indexes: frozenset[int] = frozenset()) -> None:
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
        self._fail = set(fail_batch_indexes)

    def submit(self, *, filename: str, content_type: str, data: bytes) -> ProviderTask:
        self.submitted.append(filename)
        return ProviderTask(batch_id=f"batch-{len(self.submitted)}")

    def fetch(
        self, batch_id: str, *, deadline: float | None = None, title: str = ""
    ) -> ParsedDocument:
        self.fetched.append(batch_id)
        index = int(batch_id.split("-")[1])
        if index in self._fail:
            raise ParserError("parse_failed")
        # 每个分片产出**结构化块**（ADR-038）：页号是相对本分片的 0-based 值，
        # 由摄取层按分片页范围换算成绝对页码。同时带页范围标记，便于验证首/中/尾可检索。
        match = re.search(r"\.p(\d+)-(\d+)\.", title)
        label = f"page-{match.group(1)}-{match.group(2)}" if match else "single"
        blocks = [
            DocumentBlock(
                text=f"{label} 概述", block_type="heading", page_index=0, heading_level=1, order=0
            )
        ]
        blocks.extend(
            DocumentBlock(
                text=f"{label}-token-{position}",
                block_type="paragraph",
                page_index=min(position // 40, 3),
                heading_level=None,
                order=position + 1,
            )
            for position in range(120)
        )
        blocks.append(
            DocumentBlock(
                text=f"TAIL-MARKER-{label}",
                block_type="paragraph",
                page_index=3,
                heading_level=None,
                order=121,
            )
        )
        body = normalize_text("\n\n".join(block.text for block in blocks))
        return ParsedDocument(
            text=f"# {title}\n\n{body}\n",
            title=title,
            parser=self.provider_name,
            pages=0,
            truncated=False,
            blocks=tuple(blocks),
        )


@pytest.fixture(scope="module")
def harness(live_settings):
    from backend.api.dependencies import build_container

    settings = live_settings
    container = build_container(settings)
    user = container.runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    if user is None:
        user = container.runtime.auth.create_user(
            username=settings.auth_bootstrap_username,
            password=settings.auth_bootstrap_password.get_secret_value(),
            display_name=settings.auth_bootstrap_display_name,
        )
    created: list[str] = []
    yield {"container": container, "user": user, "settings": settings, "created": created}
    for document_id in created:
        try:
            outcome = container.runtime.ingestion.submit_delete(
                user_id=user.user_id, document_id=document_id
            )
            container.runtime.ingestion.process_job(outcome.ingestion_job_id)
        except Exception as error:  # noqa: BLE001 - 清理失败不得影响测试结论
            print(f"shard_fixture_cleanup_failed: {document_id[:8]} {type(error).__name__}")
    container.close()


def _sharded_service(harness, remote: _RecordingMinerU):
    """把容器的摄取服务切到「MinerU 档位 + 记录型远端」。"""

    service = harness["container"].runtime.ingestion
    service.settings = harness["settings"].model_copy(update={"parser_provider": "mineru"})
    service.parser = RoutedParser(
        local=LightweightParser(
            max_bytes=service.settings.max_document_bytes,
            max_chars=service.settings.max_extracted_chars,
        ),
        remote=remote,
    )
    return service


def _submit_pdf(harness, service, *, label: str):
    pdf = _blank_pdf(313, seed=f"{label}-{uuid.uuid4().hex}")
    outcome = service.submit_upload(
        user_id=harness["user"].user_id,
        filename=f"{label}-{uuid.uuid4().hex[:8]}.pdf",
        content_type="application/pdf",
        stored=harness["container"].runtime.upload_storage.store_bytes(pdf),
    )
    harness["created"].append(outcome.document_id)
    return outcome


@pytest.mark.live_db
def test_a_313_page_pdf_is_sharded_promoted_and_head_middle_tail_are_searchable(harness):
    container = harness["container"]
    remote = _RecordingMinerU()
    service = _sharded_service(harness, remote)
    outcome = _submit_pdf(harness, service, label="shard-ok")

    assert service.process_job(outcome.ingestion_job_id) == "success"

    shards = container.runtime.shards.list_for_version(outcome.index_version)
    assert [(row.page_start, row.page_end) for row in shards] == [(1, 200), (201, 313)]
    assert all(row.status == "success" for row in shards)
    assert all(row.reused is False for row in shards)
    assert len(remote.submitted) == 2

    version = container.runtime.versions.get(outcome.index_version)
    assert (
        version is not None and version.status == "active" and version.parser_provider == "mineru"
    )
    document = container.runtime.documents.get(outcome.document_id, user_id=harness["user"].user_id)
    assert document is not None and document.active_index_version == outcome.index_version

    # 首/中/尾三段文本都真的进入了 child chunk。
    children = container.runtime.chunks.list_by_ids(
        [
            f"{outcome.index_version}-c{index}"
            for index in range(
                container.runtime.chunks.count_for_version(
                    outcome.index_version, chunk_role="child"
                )
            )
        ],
        index_version=outcome.index_version,
    )
    merged = "\n".join(record.content for record in children)
    assert "page-1-200-token-0" in merged
    assert "page-1-200-token-60" in merged
    assert "page-201-313-token-119" in merged

    # 结构元数据真的落库了（ADR-038）：真实页码来自 provider 的 page_idx + 分片页偏移，
    # 章节路径来自标题层级；没有来源时才会是 None。
    starts = {row.page_start for row in children}
    assert 1 in starts, "first shard pages must start at the absolute page 1"
    assert 201 in starts, "second shard pages must be offset to the absolute page 201"
    sections = " ".join(str(row.section_path or "") for row in children)
    assert "page-1-200 概述" in sections
    assert "page-201-313 概述" in sections
    assert all(row.content_hash for row in children)
    assert all(row.token_count > 0 for row in children)


@pytest.mark.live_db
def test_successful_shards_are_reused_on_reindex_without_calling_the_provider(harness):
    container = harness["container"]
    remote = _RecordingMinerU()
    service = _sharded_service(harness, remote)
    outcome = _submit_pdf(harness, service, label="shard-reuse")
    assert service.process_job(outcome.ingestion_job_id) == "success"
    submits_after_ingest = len(remote.submitted)

    reindex = service.submit_reindex(
        user_id=harness["user"].user_id, document_id=outcome.document_id
    )
    assert service.process_job(reindex.ingestion_job_id) == "success"

    # 复用键命中：新版本的两片全部 reused，且没有新的 provider 调用。
    reused = container.runtime.shards.list_for_version(reindex.index_version)
    assert len(reused) == 2
    assert all(row.reused is True for row in reused)
    assert len(remote.submitted) == submits_after_ingest


@pytest.mark.live_db
def test_a_failed_shard_blocks_promotion_and_is_retried_alone(harness):
    container = harness["container"]
    # 第二个分片（201–313）在第一次执行时失败。
    remote = _RecordingMinerU(fail_batch_indexes=frozenset({2}))
    service = _sharded_service(harness, remote)
    outcome = _submit_pdf(harness, service, label="shard-fail")

    assert service.process_job(outcome.ingestion_job_id) == "failed"
    job = container.runtime.jobs.get(outcome.ingestion_job_id)
    assert job is not None and job.failure_code == "parse_failed"
    version = container.runtime.versions.get(outcome.index_version)
    assert version is not None and version.status == "failed"
    # 缺片/失败片不得 promote：文档没有 active 版本。
    document = container.runtime.documents.get(outcome.document_id, user_id=harness["user"].user_id)
    assert document is not None and document.active_index_version is None
    statuses = {
        row.shard_index: row.status
        for row in container.runtime.shards.list_for_version(outcome.index_version)
    }
    assert statuses == {0: "success", 1: "failed"}

    submits_before_retry = len(remote.submitted)
    retry = service.submit_retry(
        user_id=harness["user"].user_id, ingestion_job_id=outcome.ingestion_job_id
    )
    assert service.process_job(retry.ingestion_job_id) == "success"

    retried = container.runtime.shards.list_for_version(retry.index_version)
    # 成功的第一片复用，只有失败的第二片重新提交。
    assert [row.reused for row in retried] == [True, False]
    assert len(remote.submitted) == submits_before_retry + 1
    document = container.runtime.documents.get(outcome.document_id, user_id=harness["user"].user_id)
    assert document is not None and document.active_index_version == retry.index_version


@pytest.mark.live_db
def test_parent_context_expands_real_parents_from_the_live_index(harness):
    """父块展开在真实库上取到的是**真父块**：同版本、parent 角色、按 ID 去重。"""

    container = harness["container"]
    remote = _RecordingMinerU()
    service = _sharded_service(harness, remote)
    outcome = _submit_pdf(harness, service, label="parent-ctx")
    assert service.process_job(outcome.ingestion_job_id) == "success"

    child_count = container.runtime.chunks.count_for_version(
        outcome.index_version, chunk_role="child"
    )
    children = container.runtime.chunks.list_by_ids(
        [f"{outcome.index_version}-c{index}" for index in range(min(4, child_count))],
        index_version=outcome.index_version,
    )
    provider = RegistryParentContext(
        registry=container.runtime.registry, max_parents=harness["settings"].max_parent_expansion
    )
    context = provider.parents_for(
        index_version=outcome.index_version, child_ids=[row.chunk_id for row in children]
    )

    assert context, "a sharded document must expose its parent chunks"
    expected = {row.parent_chunk_id for row in children if row.parent_chunk_id}
    assert set(context) <= expected
    for parent in context.values():
        assert parent["content"]
        assert parent["section_path"]  # 结构化分块给了真实章节路径
        assert isinstance(parent["page_start"], int)  # 真实页码，而不是伪造的 1
