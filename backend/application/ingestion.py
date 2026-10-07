"""文档摄取、重建索引与删除的生命周期（ADR-009 / ADR-019）。

摄取在后台 worker 中运行。Parse -> 存储归一化文本 -> Parent/Child
chunk -> embed -> 向量写入 -> validate -> promote。新的 index version 在完全
完成校验之前，绝不替换 active 版本。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

from backend.application.index_registry import IndexRegistry
from backend.domain.errors import conflict, not_found, validation_error
from backend.infrastructure.config import Settings
from backend.infrastructure.embedding import EmbeddingProvider, EmbeddingProviderError
from backend.infrastructure.parsers import (
    DocumentParser,
    ParserError,
    chunk_document,
    supported_content_type,
)
from backend.infrastructure.repositories.documents import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCESS,
    ChunkRecord,
    ChunkRepository,
    DocumentContentRepository,
    DocumentRepository,
    IndexVersionRepository,
    IngestionJobRecord,
    IngestionJobRepository,
)
from backend.infrastructure.vector_store import (
    VectorRecord,
    VectorStore,
    VectorStoreError,
    collection_name,
)

logger = logging.getLogger(__name__)

MAX_EXTRACTED_CHARS = 400_000

# Milvus 在写入后需要短暂时间才达到一致；一段简短的、有界的等待可避免
# promote 与向量可见性发生竞争。
VECTOR_VISIBILITY_SECONDS = 2.0


@dataclass(frozen=True)
class UploadOutcome:
    document_id: str
    ingestion_job_id: str
    index_version: str
    status: str
    duplicate: bool


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        documents: DocumentRepository,
        contents: DocumentContentRepository,
        versions: IndexVersionRepository,
        chunks: ChunkRepository,
        jobs: IngestionJobRepository,
        registry: IndexRegistry,
        parser: DocumentParser,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
    ) -> None:
        self.settings = settings
        self.documents = documents
        self.contents = contents
        self.versions = versions
        self.chunks = chunks
        self.jobs = jobs
        self.registry = registry
        self.parser = parser
        self.embeddings = embeddings
        self.vectors = vectors

    # 提交 ------------------------------------------------------------
    def submit_upload(
        self,
        *,
        user_id: str,
        filename: str,
        content_type: str,
        data: bytes,
        idempotency_key: str | None = None,
    ) -> UploadOutcome:
        if not data:
            raise validation_error("The uploaded file is empty.")
        if len(data) > self.settings.max_document_bytes:
            raise validation_error("The uploaded file exceeds the configured size limit.")
        if not supported_content_type(content_type, filename):
            raise validation_error("This document format is not supported.")

        fingerprint = hashlib.sha256(data).hexdigest()
        existing = self.documents.find_by_fingerprint(user_id=user_id, fingerprint=fingerprint)
        if existing is not None:
            if idempotency_key:
                prior_job = self.jobs.find_by_idempotency_key(
                    document_id=existing.document_id, idempotency_key=idempotency_key
                )
                if prior_job is not None:
                    return UploadOutcome(
                        existing.document_id,
                        prior_job.ingestion_job_id,
                        prior_job.index_version,
                        prior_job.status,
                        True,
                    )
            # 该内容指纹对应的 Document 已存在。重复提交直接由既有状态应答：
            # 不创建第二个 Document、不创建第二个 index version，
            # 也不生成一个从未 promote 的 version 记录。
            return UploadOutcome(
                existing.document_id,
                "",
                existing.active_index_version or "",
                JOB_STATUS_SUCCESS,
                True,
            )

        document = self.documents.create(
            user_id=user_id,
            title=filename,
            source_name=filename,
            content_type=content_type or "application/octet-stream",
            content_fingerprint=fingerprint,
            byte_size=len(data),
        )
        version = self._create_version(document.document_id)
        job = self.jobs.create(
            document_id=document.document_id,
            index_version=version.index_version,
            job_type="ingest",
            idempotency_key=idempotency_key or fingerprint[:80],
        )
        return UploadOutcome(
            document.document_id, job.ingestion_job_id, version.index_version, job.status, False
        )

    def submit_reindex(self, *, user_id: str, document_id: str) -> UploadOutcome:
        document = self.documents.get(document_id, user_id=user_id)
        if document is None or document.status != "available":
            raise not_found("Document was not found.")
        for job in self.jobs.list_for_document(document_id):
            if job.status in {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}:
                raise conflict("An ingestion job is already active for this document.")
        if self.contents.get(document_id) is None:
            raise conflict("The document has no stored content to re-index.")
        latest = self.jobs.max_attempt(document_id)
        version = self._create_version(document_id)
        job = self.jobs.create(
            document_id=document_id,
            index_version=version.index_version,
            job_type="reindex",
            attempt=latest + 1,
        )
        return UploadOutcome(
            document_id, job.ingestion_job_id, version.index_version, job.status, False
        )

    def submit_delete(self, *, user_id: str, document_id: str) -> UploadOutcome:
        document = self.documents.get(document_id, user_id=user_id)
        if document is None or document.status != "available":
            raise not_found("Document was not found.")
        # 在清理之前先标记为不可用，这样 promote 就无法将其重新激活。
        self.documents.mark_deleted(document_id, user_id=user_id)
        version = self._create_version(document_id)
        job = self.jobs.create(
            document_id=document_id,
            index_version=version.index_version,
            job_type="delete",
        )
        return UploadOutcome(
            document_id, job.ingestion_job_id, version.index_version, job.status, False
        )

    def submit_retry(self, *, user_id: str, ingestion_job_id: str) -> UploadOutcome:
        job = self.jobs.get(ingestion_job_id)
        if job is None:
            raise not_found("Ingestion job was not found.")
        document = self.documents.get(job.document_id, user_id=user_id)
        if document is None:
            raise not_found("Document was not found.")
        if job.status != JOB_STATUS_FAILED:
            raise conflict("Only failed ingestion jobs can be retried.")
        version = self._create_version(job.document_id)
        latest = self.jobs.max_attempt(job.document_id)
        new_job = self.jobs.create(
            document_id=job.document_id,
            index_version=version.index_version,
            job_type=job.job_type,
            attempt=latest + 1,
        )
        return UploadOutcome(
            job.document_id, new_job.ingestion_job_id, version.index_version, new_job.status, False
        )

    def get_job(self, *, user_id: str, ingestion_job_id: str) -> IngestionJobRecord:
        job = self.jobs.get(ingestion_job_id)
        if job is None:
            raise not_found("Ingestion job was not found.")
        if self.documents.get(job.document_id, user_id=user_id) is None:
            raise not_found("Ingestion job was not found.")
        return job

    # Worker 处理 ----------------------------------------------------------------
    def process_job(self, ingestion_job_id: str, *, payload: bytes | None = None) -> str:
        job = self.jobs.get(ingestion_job_id)
        if job is None:
            return JOB_STATUS_FAILED
        if not self.jobs.claim(ingestion_job_id):
            # 已有其他 worker 抢先 claim；claim 操作是幂等的。
            current = self.jobs.get(ingestion_job_id)
            return current.status if current else JOB_STATUS_FAILED
        if job.job_type == "delete":
            return self._run_delete(job)
        try:
            text = self._extract_text(job, payload)
            chunk_count = self._index_text(job, text)
        except _IngestionFailure as failure:
            self.versions.mark_failed(job.index_version, failure_code=failure.code)
            self.jobs.finish(
                ingestion_job_id,
                status=JOB_STATUS_FAILED,
                failure_code=failure.code,
                failure_message=failure.safe_message,
            )
            return JOB_STATUS_FAILED
        except Exception as error:  # noqa: BLE001 - worker must not crash the app
            logger.warning("ingestion_unexpected_failure: %s", type(error).__name__)
            self.versions.mark_failed(job.index_version, failure_code="ingestion_error")
            self.jobs.finish(
                ingestion_job_id,
                status=JOB_STATUS_FAILED,
                failure_code="ingestion_error",
                failure_message="Ingestion could not complete safely.",
            )
            return JOB_STATUS_FAILED
        self.jobs.finish(ingestion_job_id, status=JOB_STATUS_SUCCESS)
        logger.info("ingestion_succeeded chunks=%s", chunk_count)
        return JOB_STATUS_SUCCESS

    # 内部实现 -------------------------------------------------------------
    def _create_version(self, document_id: str):
        provider = self.embeddings.provider_name
        model = self.embeddings.model
        dimension = self.embeddings.dimension
        return self.versions.create(
            document_id=document_id,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dimension=dimension,
            collection_name=collection_name(provider=provider, model=model, dimension=dimension),
            parser_provider=self.parser.provider_name,
        )

    def _extract_text(self, job: IngestionJobRecord, payload: bytes | None) -> str:
        if payload is None:
            # 重建索引、重试的 Job，以及任何在缺少原始字节情况下恢复的 Job，
            # 都使用首次摄取时捕获的归一化文本。
            stored = self.contents.get(job.document_id)
            if stored is None:
                raise _IngestionFailure("content_unavailable", "Stored document text is missing.")
            return stored
        document = self.documents.get(job.document_id)
        filename = document.source_name if document else "document.txt"
        content_type = document.content_type if document else "text/plain"
        try:
            parsed = self.parser.parse(filename=filename, content_type=content_type, data=payload)
        except ParserError as error:
            raise _IngestionFailure(str(error), "The document could not be parsed.") from error
        text = parsed.text[:MAX_EXTRACTED_CHARS]
        self.contents.upsert(
            document_id=job.document_id,
            extracted_text=text,
            parser_provider=parsed.parser,
            truncated=parsed.truncated or len(parsed.text) > MAX_EXTRACTED_CHARS,
        )
        return text

    def _index_text(self, job: IngestionJobRecord, text: str) -> int:
        chunked = chunk_document(
            text,
            parent_chars=self.settings.parent_chunk_chars,
            child_chars=self.settings.chunk_size_chars,
            overlap_chars=self.settings.chunk_overlap_chars,
        )
        if not chunked.children:
            raise _IngestionFailure("no_extractable_text", "No indexable content was produced.")

        document_id = job.document_id
        index_version = job.index_version
        labels = {
            parent.ordinal: _label_for(text, parent.content, parent.ordinal)
            for parent in chunked.parents
        }
        parent_ids = {
            parent.ordinal: f"{index_version}-p{parent.ordinal}" for parent in chunked.parents
        }
        records: list[ChunkRecord] = [
            ChunkRecord(
                chunk_id=parent_ids[parent.ordinal],
                document_id=document_id,
                index_version=index_version,
                parent_chunk_id=None,
                chunk_role="parent",
                ordinal=parent.ordinal,
                content=parent.content,
                location=None,
                label=labels[parent.ordinal],
            )
            for parent in chunked.parents
        ]
        child_records = [
            ChunkRecord(
                chunk_id=f"{index_version}-c{child.ordinal}",
                document_id=document_id,
                index_version=index_version,
                parent_chunk_id=parent_ids[child.parent_ordinal],
                chunk_role="child",
                ordinal=child.ordinal,
                content=child.content,
                location=f"chunk-{child.ordinal}",
                label=labels[child.parent_ordinal],
            )
            for child in chunked.children
        ]
        records.extend(child_records)
        self.chunks.delete_for_version(index_version)
        self.chunks.add_many(records)

        try:
            vectors = self.embeddings.embed_many([child.content for child in chunked.children])
        except EmbeddingProviderError as error:
            raise _IngestionFailure(str(error), "Embedding could not be produced.") from error
        if len(vectors) != len(child_records):
            raise _IngestionFailure("embedding_count_mismatch", "Embedding output was incomplete.")

        collection = collection_name(
            provider=self.embeddings.provider_name,
            model=self.embeddings.model,
            dimension=len(vectors[0]),
        )
        try:
            self.vectors.ensure_collection(name=collection, dimension=len(vectors[0]))
            self.vectors.upsert(
                collection=collection,
                records=[
                    VectorRecord(
                        chunk_id=record.chunk_id,
                        document_id=document_id,
                        index_version=index_version,
                        parent_chunk_id=record.parent_chunk_id,
                        content=record.content,
                        label=record.label,
                        location=record.location,
                        embedding=vector,
                    )
                    for record, vector in zip(child_records, vectors, strict=True)
                ],
            )
            # 向量可见性并非瞬时，因此下面的校验不能与写入竞争；
            # 未通过校验的向量不会被 promote。
            time.sleep(VECTOR_VISIBILITY_SECONDS)
            self._verify_vectors(
                collection=collection, index_version=index_version, expected=len(child_records)
            )
        except VectorStoreError as error:
            raise _IngestionFailure(str(error), "The vector store could not be written.") from error

        self.versions.mark_validated(index_version, chunk_count=len(child_records))
        if not self.registry.promote(index_version):
            raise _IngestionFailure(
                "document_not_available", "The document is no longer available."
            )
        return len(child_records)

    def _verify_vectors(self, *, collection: str, index_version: str, expected: int) -> None:
        """只有在向量的可校验性得到确认之后，新的 index version 才会被 promote。"""

        if expected <= 0:
            return
        try:
            matches = self.vectors.search(
                collection=collection,
                embedding=self.embeddings.embed("index verification probe"),
                top_k=min(expected, self.settings.max_retrieval_top_k),
                index_version=index_version,
            )
        except (VectorStoreError, EmbeddingProviderError) as error:
            raise _IngestionFailure(
                "vector_verification_failed", "The new index version could not be verified."
            ) from error
        if not matches:
            raise _IngestionFailure(
                "vector_verification_failed", "The new index version could not be verified."
            )

    def _run_delete(self, job: IngestionJobRecord) -> str:
        try:
            for version in self.versions.list_for_document(job.document_id):
                self._delete_version_vectors(version.index_version, version.collection_name)
                self.chunks.delete_for_version(version.index_version)
            self.documents.set_active_version(job.document_id, None)
            self.jobs.finish(job.ingestion_job_id, status=JOB_STATUS_SUCCESS)
            return JOB_STATUS_SUCCESS
        except VectorStoreError as error:
            self.jobs.finish(
                job.ingestion_job_id,
                status=JOB_STATUS_FAILED,
                failure_code="vector_cleanup_failed",
                failure_message="Vector cleanup could not complete.",
            )
            logger.warning("delete_cleanup_failed: %s", type(error).__name__)
            return JOB_STATUS_FAILED

    def _delete_version_vectors(self, index_version: str, collection: str) -> None:
        if not collection:
            return
        try:
            self.vectors.delete_version(collection=collection, index_version=index_version)
        except VectorStoreError:
            # Document 已处于不可用状态，因此清理失败会被上报，
            # 绝不静默当作删除已完成。
            raise

    def recover_stale_jobs(self) -> dict[str, int]:
        stale_running = self.jobs.fail_stale_running(
            older_than=_seconds_ago(self.settings.ingestion_stale_running_seconds)
        )
        orphaned_pending = self.jobs.fail_orphaned_pending(
            older_than=_seconds_ago(self.settings.ingestion_orphaned_pending_seconds)
        )
        return {"stale_running": stale_running, "orphaned_pending": orphaned_pending}


class _IngestionFailure(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(code)
        self.code = code[:64]
        self.safe_message = safe_message[:500]


def _seconds_ago(seconds: int):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    return utc_now() - timedelta(seconds=seconds)


def _label_for(text: str, block: str, ordinal: int) -> str:
    first_line = block.strip().splitlines()[0] if block.strip() else f"Section {ordinal + 1}"
    return first_line[:200]


__all__ = ["IngestionService", "UploadOutcome"]
