"""文档摄取、重建索引与删除的生命周期（ADR-009 / ADR-019）。

摄取在后台 worker 中运行。Parse -> 存储归一化文本 -> Parent/Child
chunk -> embed -> 向量写入 -> validate -> promote。新的 index version 在完全
完成校验之前，绝不替换 active 版本。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from backend.application.index_registry import IndexRegistry
from backend.application.lease import LeaseHeartbeat
from backend.domain.errors import ExecutionOwnershipLost, conflict, not_found, validation_error
from backend.domain.identifiers import new_id
from backend.infrastructure.config import Settings
from backend.infrastructure.embedding import EmbeddingProvider, EmbeddingProviderError
from backend.infrastructure.file_storage import (
    ManagedUploadStorage,
    StoredUpload,
    UploadStorageError,
)
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
    DocumentFileRepository,
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

# Milvus 在写入后需要短暂时间才达到一致；一段简短的、有界的等待可避免
# promote 与向量可见性发生竞争。
VECTOR_VISIBILITY_SECONDS = 2.0

# Chunk 行的批量写入边界：大文档不再产生单条无界的 SQL 语句。
CHUNK_WRITE_BATCH = 500


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
        files: DocumentFileRepository,
        storage: ManagedUploadStorage,
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
        self.files = files
        # 原文件受控存储：解析、重试与后续分片都从这里取字节，绝不由调用方传内存副本。
        self.storage = storage
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
        stored: StoredUpload,
        idempotency_key: str | None = None,
    ) -> UploadOutcome:
        """登记一份**已经写入受控存储**的原文件并创建持久 Job。

        原文件在上传期间已流式落盘（``ManagedUploadStorage``），这里只做校验与登记：
        - 重复内容（同一 sha256）不创建第二个 Document/version，并复用既有行；
        - 若既有 Document 尚无原文件行（历史数据），本次上传会补齐该行，
          使重试与后续分片都能取到原始字节；
        - 登记完成后 Job 处于 ``pending``：即使调度容量一时耗尽，原文件与 Job 也已持久化。
        """

        fingerprint = stored.sha256
        if stored.byte_size <= 0:
            raise validation_error("The uploaded file is empty.")
        if stored.byte_size > self.settings.max_document_bytes:
            raise validation_error("The uploaded file exceeds the configured size limit.")
        if not supported_content_type(
            content_type, filename, allow_remote_parser=self.settings.parser_provider == "mineru"
        ):
            raise validation_error("This document format is not supported.")

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
            # 不创建第二个 Document、不创建第二个 index version；
            # 若既有文档缺少原文件行（早于受控存储引入），顺手补齐，避免留下无主文件。
            if self.files.get(existing.document_id) is None:
                self.files.upsert(
                    document_id=existing.document_id,
                    storage_key=stored.storage_key,
                    sha256=fingerprint,
                    byte_size=stored.byte_size,
                    content_type=content_type or "application/octet-stream",
                    source_name=filename,
                )
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
            byte_size=stored.byte_size,
        )
        self.files.upsert(
            document_id=document.document_id,
            storage_key=stored.storage_key,
            sha256=fingerprint,
            byte_size=stored.byte_size,
            content_type=content_type or "application/octet-stream",
            source_name=filename,
        )
        version = self._create_version(
            document.document_id,
            parser_provider=self._effective_parser_provider(
                filename=filename, content_type=content_type or "application/octet-stream"
            ),
        )
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
        version = self._create_version(
            document_id, parser_provider=self._document_parser_provider(document_id)
        )
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
        version = self._create_version(
            document_id, parser_provider=self._document_parser_provider(document_id)
        )
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
        version = self._create_version(
            job.document_id, parser_provider=self._document_parser_provider(job.document_id)
        )
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
    def process_job(self, ingestion_job_id: str) -> str:
        """执行一个摄取 Job（claim -> lease 心跳 -> 工作 -> 所有权条件收尾）。

        执行所有权（ADR-035）：claim 记录本次 ``owner_token``；独立心跳线程按
        ``INGESTION_HEARTBEAT_SECONDS`` 续租，不依赖解析/嵌入等步骤的边界；
        promote、删除清理与终态写入都在提交点校验所有权，失去所有权的旧 worker
        不写入任何数据。
        """
        job = self.jobs.get(ingestion_job_id)
        if job is None:
            return JOB_STATUS_FAILED
        owner_token = new_id()
        if not self.jobs.claim(ingestion_job_id, owner_token=owner_token):
            # 已有其他 worker 抢先 claim；claim 操作是幂等的。
            current = self.jobs.get(ingestion_job_id)
            return current.status if current else JOB_STATUS_FAILED
        heartbeat = LeaseHeartbeat(
            interval_seconds=self.settings.ingestion_heartbeat_seconds,
            renew=lambda: self.jobs.heartbeat(ingestion_job_id, owner_token=owner_token),
            name=f"ingestion-heartbeat-{ingestion_job_id[:8]}",
        )
        heartbeat.start()
        try:
            if job.job_type == "delete":
                return self._run_delete(job, owner_token=owner_token, heartbeat=heartbeat)
            try:
                text = self._extract_text(job)
                chunk_count = self._index_text(job, text, owner_token=owner_token)
            except ExecutionOwnershipLost:
                # 租约已被撤销（陈旧恢复接管）：不写版本、不写终态，由恢复路径收口。
                heartbeat.mark_lost()
                logger.info("ingestion_ownership_lost job=%s", ingestion_job_id)
                return self._current_status(ingestion_job_id)
            except _IngestionFailure as failure:
                self._record_failure(
                    job, owner_token=owner_token, code=failure.code, message=failure.safe_message
                )
                return JOB_STATUS_FAILED
            except Exception as error:  # noqa: BLE001 - worker must not crash the app
                logger.warning("ingestion_unexpected_failure: %s", type(error).__name__)
                self._record_failure(
                    job,
                    owner_token=owner_token,
                    code="ingestion_error",
                    message="Ingestion could not complete safely.",
                )
                return JOB_STATUS_FAILED
            if not self.jobs.finish(
                ingestion_job_id, status=JOB_STATUS_SUCCESS, owner_token=owner_token
            ):
                # 丢失所有权：终态与成功日志都不得写入（恢复路径已给出权威状态）。
                logger.info("ingestion_finish_skipped_ownership_lost job=%s", ingestion_job_id)
                return self._current_status(ingestion_job_id)
            logger.info("ingestion_succeeded chunks=%s", chunk_count)
            return JOB_STATUS_SUCCESS
        finally:
            heartbeat.stop()

    # 内部实现 -------------------------------------------------------------
    def _record_failure(
        self, job: IngestionJobRecord, *, owner_token: str, code: str, message: str
    ) -> None:
        """记录一次有界失败：先赢下 Job 终态的所有权条件写，再标记版本失败。

        顺序保证「失去所有权的旧 worker」不留下任何痕迹：条件更新失败时不写版本，
        恢复路径已经给出权威的失败状态与失败码。
        """

        finished = self.jobs.finish(
            job.ingestion_job_id,
            status=JOB_STATUS_FAILED,
            failure_code=code,
            failure_message=message,
            owner_token=owner_token,
        )
        if finished:
            self.versions.mark_failed(job.index_version, failure_code=code)

    def _current_status(self, ingestion_job_id: str) -> str:
        """返回权威的当前 Job 状态（放弃写入时用它向调用方报告真实结果）。"""

        current = self.jobs.get(ingestion_job_id)
        return current.status if current else JOB_STATUS_FAILED

    def _create_version(self, document_id: str, *, parser_provider: str | None = None):
        provider = self.embeddings.provider_name
        model = self.embeddings.model
        dimension = self.embeddings.dimension
        return self.versions.create(
            document_id=document_id,
            embedding_provider=provider,
            embedding_model=model,
            embedding_dimension=dimension,
            collection_name=collection_name(provider=provider, model=model, dimension=dimension),
            # 记录**真正解析该文件**的 provider（路由档位下文本类走本地 parser，ADR-036）。
            parser_provider=parser_provider or self.parser.provider_name,
        )

    def _effective_parser_provider(self, *, filename: str, content_type: str) -> str:
        provider_for = getattr(self.parser, "provider_for", None)
        if callable(provider_for):
            return str(provider_for(filename=filename, content_type=content_type))
        return self.parser.provider_name

    def _document_parser_provider(self, document_id: str) -> str | None:
        """按文档自身的文件名/类型计算生效 provider（重建索引/重试用）。"""

        document = self.documents.get(document_id)
        if document is None:
            return None
        return self._effective_parser_provider(
            filename=document.source_name, content_type=document.content_type
        )

    def _extract_text(self, job: IngestionJobRecord) -> str:
        """优先用受控存储中的原文件解析；历史数据退回首次摄取保存的归一化文本。

        原文件在 P1 起被持久化，因此「解析失败的 Job 重试」不再因为没有原始字节而必然
        失败；只有早于受控存储的旧文档才会走到兜底路径。

        文本长度是**显式预算**（ADR-036 决策 2）：超过 ``max_extracted_chars`` 时明确失败，
        绝不写入被截断的前缀冒充全文。
        """

        document = self.documents.get(job.document_id)
        filename = document.source_name if document else "document.txt"
        content_type = document.content_type if document else "text/plain"
        # 读取上限按**生效 parser** 的有界预算决定，绝不把必然无法解析的大文件读进内存。
        original = self._load_original_bytes(
            job,
            max_bytes=self._parse_read_limit(filename=filename, content_type=content_type),
        )
        if original is None:
            stored = self.contents.get(job.document_id)
            if stored is None:
                raise _IngestionFailure("content_unavailable", "Stored document text is missing.")
            return stored
        try:
            parsed = self.parser.parse(filename=filename, content_type=content_type, data=original)
        except ParserError as error:
            raise _IngestionFailure(str(error), _parser_failure_message(str(error))) from error
        text = parsed.text
        if not text.strip():
            raise _IngestionFailure("no_extractable_text", "No indexable content was produced.")
        if len(text) > self.settings.max_extracted_chars:
            raise _IngestionFailure(
                "extracted_text_too_large",
                "The extracted text exceeds the configured budget and was not indexed.",
            )
        self.contents.upsert(
            document_id=job.document_id,
            extracted_text=text,
            # 解析器实际使用的 provider（路由档位下文本类走本地 parser，ADR-036 决策 5）。
            parser_provider=parsed.parser,
            truncated=False,
        )
        return text

    def _parse_read_limit(self, *, filename: str, content_type: str) -> int:
        """按生效 parser 决定读取上限，避免把无法解析的大文件整体读入内存。

        - 远端 provider：单份硬限（``MINERU_MAX_BYTES``）——超过它的单文件本来就会被
          拒绝（分片在 P2 batch 2 落实）；
        - 本地文本 parser：按字符预算换算有界字节上限（UTF-8 最多 4 字节/字符 + 余量）。
        """

        provider = self._effective_parser_provider(filename=filename, content_type=content_type)
        if provider == "mineru":
            return min(self.settings.max_document_bytes, self.settings.mineru_max_bytes)
        text_bytes = self.settings.max_extracted_chars * 4 + 1_048_576
        return min(self.settings.max_document_bytes, text_bytes)

    def _load_original_bytes(self, job: IngestionJobRecord, *, max_bytes: int) -> bytes | None:
        """从受控存储读取原文件；缺失或不可读时返回 ``None``（由调用方退回已存文本）。

        超过 ``max_bytes``（生效 parser 的有界预算）时明确失败，绝不整体读入内存。
        """

        record = self.files.get(job.document_id)
        if record is None:
            return None
        try:
            return self.storage.read_bytes(record.storage_key, max_bytes=max_bytes)
        except UploadStorageError as error:
            if str(error) == "stored_file_too_large":
                raise _IngestionFailure(
                    "document_too_large",
                    "The document exceeds the parser's bounded read limit.",
                ) from error
            logger.warning("original_file_unavailable: %s", type(error).__name__)
            return None

    def _index_text(self, job: IngestionJobRecord, text: str, *, owner_token: str) -> int:
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
        # 分块行按批写入：大文档不再产生单条无界 SQL 语句（ADR-036 决策 3）。
        self.chunks.delete_for_version(index_version)
        for start in range(0, len(records), CHUNK_WRITE_BATCH):
            self.chunks.add_many(records[start : start + CHUNK_WRITE_BATCH])

        # Embedding 按批调用 provider：单次请求有界，失败仍明确（绝不部分成功冒充全文）。
        child_contents = [child.content for child in chunked.children]
        vectors: list[list[float]] = []
        batch_size = max(1, self.settings.embedding_batch_size)
        try:
            for start in range(0, len(child_contents), batch_size):
                batch = child_contents[start : start + batch_size]
                batch_vectors = self.embeddings.embed_many(batch)
                if len(batch_vectors) != len(batch):
                    raise _IngestionFailure(
                        "embedding_count_mismatch", "Embedding output was incomplete."
                    )
                vectors.extend(batch_vectors)
        except EmbeddingProviderError as error:
            raise _IngestionFailure(str(error), "Embedding could not be produced.") from error
        if len(vectors) != len(child_records) or not vectors:
            raise _IngestionFailure("embedding_count_mismatch", "Embedding output was incomplete.")
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise _IngestionFailure(
                "embedding_dimension_mismatch", "Embedding output was inconsistent."
            )

        collection = collection_name(
            provider=self.embeddings.provider_name,
            model=self.embeddings.model,
            dimension=dimension,
        )
        try:
            self.vectors.ensure_collection(name=collection, dimension=dimension)
            vector_batch = max(1, self.settings.vector_upsert_batch_size)
            for start in range(0, len(child_records), vector_batch):
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
                        for record, vector in zip(
                            child_records[start : start + vector_batch],
                            vectors[start : start + vector_batch],
                            strict=True,
                        )
                    ],
                )
            # 向量可见性并非瞬时，因此下面的校验不能与写入竞争；
            # 未通过校验的向量不会被 promote。
            time.sleep(VECTOR_VISIBILITY_SECONDS)
            self._verify_completeness(
                index_version=index_version,
                document_id=document_id,
                collection=collection,
                dimension=dimension,
                parent_count=len(chunked.parents),
                child_count=len(child_records),
                chunk_ids=[record.chunk_id for record in records],
            )
            self._verify_vectors(
                collection=collection, index_version=index_version, expected=len(child_records)
            )
        except VectorStoreError as error:
            raise _IngestionFailure(str(error), "The vector store could not be written.") from error

        # 提交点：promote 之前必须仍持有执行所有权。解析、嵌入与向量写入可能耗时很长，
        # 期间陈旧恢复可能已经撤销本次执行；此时写出「已校验/已晋升」的版本与
        # registry 的 active 指针都属于失去所有权的旧执行者，绝不允许发生（ADR-035）。
        if not self.jobs.still_owned(job.ingestion_job_id, owner_token=owner_token):
            raise ExecutionOwnershipLost(job.ingestion_job_id)
        self.versions.mark_validated(index_version, chunk_count=len(child_records))
        if not self.registry.promote(index_version):
            raise _IngestionFailure(
                "document_not_available", "The document is no longer available."
            )
        return len(child_records)

    def _verify_completeness(
        self,
        *,
        index_version: str,
        document_id: str,
        collection: str,
        dimension: int,
        parent_count: int,
        child_count: int,
        chunk_ids: list[str],
    ) -> None:
        """promote 前的完整性校验（ADR-036 决策 4）。

        只看「向量条数」不足以证明内容闭环：这里核对分块行是否与预期一致（Parent/Child
        各自的数量）、chunk ID 是否唯一、embedding 维度与 provider profile 是否一致、
        collection 名是否由同一 profile 派生、以及已存文本是否非空。任一不符即明确失败，
        绝不 promote 一个不完整的版本。
        """

        if len(set(chunk_ids)) != len(chunk_ids):
            raise _IngestionFailure("chunk_id_conflict", "Chunk identities were not unique.")
        stored_parents = self.chunks.count_for_version(index_version, chunk_role="parent")
        stored_children = self.chunks.count_for_version(index_version, chunk_role="child")
        if stored_parents != parent_count or stored_children != child_count:
            raise _IngestionFailure(
                "chunk_manifest_mismatch", "The stored chunk manifest was incomplete."
            )
        if dimension != self.settings.embedding_dimension:
            raise _IngestionFailure(
                "embedding_dimension_mismatch", "The embedding profile did not match the index."
            )
        expected_collection = collection_name(
            provider=self.embeddings.provider_name,
            model=self.embeddings.model,
            dimension=dimension,
        )
        if collection != expected_collection:
            raise _IngestionFailure(
                "collection_profile_mismatch", "The vector collection profile was inconsistent."
            )
        stored_text = self.contents.get(document_id)
        if not stored_text or not stored_text.strip():
            raise _IngestionFailure("content_unavailable", "Stored document text is missing.")

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

    def _run_delete(
        self, job: IngestionJobRecord, *, owner_token: str, heartbeat: LeaseHeartbeat
    ) -> str:
        """执行删除 Job：清理向量、chunks、active 指针与原文件。

        提交点所有权校验：清理是副作用，只有仍持有执行所有权的 worker 才能执行；
        租约被撤销（心跳已发现失权，或提交点校验失败）时不清理任何数据，由恢复路径
        或显式重试收口（ADR-035）。
        """

        if heartbeat.lost or not self.jobs.still_owned(
            job.ingestion_job_id, owner_token=owner_token
        ):
            logger.info("delete_skipped_ownership_lost job=%s", job.ingestion_job_id)
            return self._current_status(job.ingestion_job_id)
        try:
            for version in self.versions.list_for_document(job.document_id):
                self._delete_version_vectors(version.index_version, version.collection_name)
                self.chunks.delete_for_version(version.index_version)
            self.documents.set_active_version(job.document_id, None)
            # 原文件在文档删除后不再需要（重试/重建索引都要求文档可用），
            # 但只有确认没有其它 Document 引用同一内容对象时才真正删除磁盘文件。
            self._delete_original_file(job.document_id)
            if not self.jobs.finish(
                job.ingestion_job_id, status=JOB_STATUS_SUCCESS, owner_token=owner_token
            ):
                return self._current_status(job.ingestion_job_id)
            return JOB_STATUS_SUCCESS
        except VectorStoreError as error:
            self.jobs.finish(
                job.ingestion_job_id,
                status=JOB_STATUS_FAILED,
                failure_code="vector_cleanup_failed",
                failure_message="Vector cleanup could not complete.",
                owner_token=owner_token,
            )
            logger.warning("delete_cleanup_failed: %s", type(error).__name__)
            return JOB_STATUS_FAILED

    def _delete_original_file(self, document_id: str) -> None:
        """删除原文件记录与受控对象（内容寻址：仍有其它引用时只删记录）。"""

        record = self.files.get(document_id)
        if record is None:
            return
        self.files.delete(document_id)
        if self.files.count_by_storage_key(record.storage_key) == 0:
            self.storage.delete(record.storage_key)

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


# 解析失败码 → 对用户安全的说明（ADR-036：失败必须明确且可区分，不伪装成通用错误）。
_PARSER_FAILURE_MESSAGES: dict[str, str] = {
    "empty_document": "The document is empty.",
    "document_too_large": "The document exceeds the local size limit.",
    "document_exceeds_parser_limit": (
        "The document exceeds the parser's per-file limit and cannot be submitted."
    ),
    "extracted_text_too_large": (
        "The extracted text exceeds the configured budget and was not indexed."
    ),
    "no_extractable_text": "No indexable text could be extracted from the document.",
    "unsupported_encoding": "The document encoding is not supported.",
    "unparseable_document": "The document could not be parsed.",
    "parser_result_too_large": "The parser result exceeded the bounded processing limits.",
    "parser_result_unsafe": "The parser result was rejected by the ingestion safety bounds.",
    "parser_unavailable": "The document parser is unavailable for this request.",
    "parse_failed": "The document could not be parsed.",
}


def _parser_failure_message(code: str) -> str:
    return _PARSER_FAILURE_MESSAGES.get(code, "The document could not be parsed.")


def _seconds_ago(seconds: int):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    return utc_now() - timedelta(seconds=seconds)


def _label_for(text: str, block: str, ordinal: int) -> str:
    first_line = block.strip().splitlines()[0] if block.strip() else f"Section {ordinal + 1}"
    return first_line[:200]


__all__ = ["IngestionService", "UploadOutcome"]
