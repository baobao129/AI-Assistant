"""文档摄取、重建索引与删除的生命周期（ADR-009 / ADR-019）。

摄取在后台 worker 中运行。Parse -> 存储归一化文本 -> Parent/Child
chunk -> embed -> 向量写入 -> validate -> promote。新的 index version 在完全
完成校验之前，绝不替换 active 版本。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, replace

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
from backend.infrastructure.parser_errors import ParserError
from backend.infrastructure.parsers import (
    DocumentBlock,
    DocumentParser,
    MinerUParser,
    ParsedDocument,
    chunk_document,
    deserialize_blocks,
    normalize_text,
    serialize_blocks,
    supported_content_type,
)
from backend.infrastructure.pdf_slicing import PageRange, count_pages, plan_shards, slice_pages
from backend.infrastructure.repositories.documents import (
    DOCUMENT_STATUS_AVAILABLE,
    DOCUMENT_STATUS_DELETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCESS,
    SHARD_STATUS_PENDING,
    SHARD_STATUS_SUCCESS,
    ChunkRecord,
    ChunkRepository,
    DocumentContentRepository,
    DocumentFileRepository,
    DocumentRepository,
    IndexVersionRepository,
    IngestionJobRecord,
    IngestionJobRepository,
    IngestionShardRepository,
)
from backend.infrastructure.structure_chunking import (
    chunk_blocks,
    content_hash,
    with_page_offset,
)
from backend.infrastructure.token_budget import estimate_tokens
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


@dataclass(frozen=True)
class _Extraction:
    """一次摄取抽取的结果：文本 + 分片/页数事实 + provider 结构化块。

    ``blocks`` 非空时走结构化分块（ADR-038）；为空时回退文本分块并记录回退。
    """

    text: str
    shard_count: int
    page_count: int
    sharded: bool
    blocks: tuple[DocumentBlock, ...] = ()
    warnings: tuple[str, ...] = ()


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
        shards: IngestionShardRepository,
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
        # 分片 manifest：复用（不重复付费解析）与恢复（续轮询同一 provider 任务）的唯一依据。
        self.shards = shards
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
            # 重复提交必须报告**真实状态**（ADR-040 决策 4 / G20）：不新建 Job、不 dispatch，
            # 但绝不能把 pending/running/failed 说成 success —— 否则客户端无法判断
            # 「已索引」「还在索引」「索引失败」，坏文档也就无法通过 API 自愈。
            latest_jobs = self.jobs.list_for_document(existing.document_id)
            if latest_jobs:
                latest = latest_jobs[0]
                return UploadOutcome(
                    existing.document_id,
                    latest.ingestion_job_id,
                    latest.index_version,
                    latest.status,
                    True,
                )
            # 没有任何 Job 的历史行（早于持久化 Job 的数据）：只能按 active 版本判断。
            usable = existing.status == DOCUMENT_STATUS_AVAILABLE and bool(
                existing.active_index_version
            )
            return UploadOutcome(
                existing.document_id,
                "",
                existing.active_index_version or "",
                JOB_STATUS_SUCCESS if usable else JOB_STATUS_FAILED,
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
        if document is None or document.status == DOCUMENT_STATUS_DELETED:
            raise not_found("Document was not found.")
        # 重建索引需要既有内容，因此要求文档已经可用（ADR-040 决策 6）；
        # 但 indexing/failed 与「不存在」是两回事，必须给出不同的解释。
        if document.status != DOCUMENT_STATUS_AVAILABLE:
            raise conflict("The document is not ready to be re-indexed.")
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
        # 任何未删除的文档都可删除（含 indexing / failed）：卡住或坏掉的文档必须能被清掉，
        # 否则用户没有任何自愈路径（ADR-040 决策 5）。
        if document is None or document.status == DOCUMENT_STATUS_DELETED:
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
    def process_job(self, ingestion_job_id: str, *, reuse_index_version: str | None = None) -> str:
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
                if reuse_index_version:
                    chunk_count = self._migrate_hybrid(job, reuse_index_version, owner_token)
                else:
                    extraction = self._extract_text(job, owner_token=owner_token)
                    chunk_count = self._index_text(job, extraction, owner_token=owner_token)
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
    def migrate_hybrid_index(self, *, user_id: str, document_id: str) -> UploadOutcome:
        """Re-index compatible active chunks/vectors without reparsing the original file."""
        document = self.documents.get(document_id, user_id=user_id)
        if document is None or document.status != DOCUMENT_STATUS_AVAILABLE:
            raise not_found("Document is unavailable.")
        source = self.versions.get(document.active_index_version or "")
        if source is None or source.status != "active":
            raise conflict("Document index is unavailable.")
        if (source.embedding_provider, source.embedding_model, source.embedding_dimension) != (
            self.embeddings.provider_name,
            self.embeddings.model,
            self.settings.embedding_dimension,
        ):
            raise conflict("Document index profile is incompatible.")
        outcome = self.submit_reindex(user_id=user_id, document_id=document_id)
        self.process_job(outcome.ingestion_job_id, reuse_index_version=source.index_version)
        return outcome

    def _migrate_hybrid(self, job: IngestionJobRecord, source_id: str, owner_token: str) -> int:
        source = self.versions.get(source_id)
        document = self.documents.get(job.document_id)
        target = self.versions.get(job.index_version)
        if (
            source is None
            or target is None
            or document is None
            or document.status != DOCUMENT_STATUS_AVAILABLE
            or document.active_index_version != source_id
            or source.status != "active"
            or source.document_id != job.document_id
        ):
            raise _IngestionFailure(
                "migration_source_unavailable", "Migration source is unavailable."
            )
        if (source.embedding_provider, source.embedding_model, source.embedding_dimension) != (
            target.embedding_provider,
            target.embedding_model,
            target.embedding_dimension,
        ):
            raise _IngestionFailure(
                "migration_profile_mismatch", "Index profiles are incompatible."
            )
        collection = target.collection_name
        self.vectors.ensure_collection(name=collection, dimension=target.embedding_dimension)
        copied_parents: set[str] = set()
        seen_children: set[str] = set()

        def clone(chunk: ChunkRecord) -> ChunkRecord:
            prefix = f"{job.index_version}-{'p' if chunk.chunk_role == 'parent' else 'c'}"
            return replace(
                chunk,
                chunk_id=f"{prefix}{chunk.ordinal}",
                index_version=job.index_version,
                parent_chunk_id=(
                    f"{job.index_version}-p{parent_ordinals[chunk.parent_chunk_id]}"
                    if chunk.parent_chunk_id
                    else None
                ),
            )

        for rows in self.vectors.version_batches(
            collection=source.collection_name, index_version=source_id, expected=source.chunk_count
        ):
            if not self.jobs.still_owned(job.ingestion_job_id, owner_token=owner_token):
                raise ExecutionOwnershipLost(job.ingestion_job_id)
            children = self.chunks.list_by_ids(
                [str(row["chunk_id"]) for row in rows], index_version=source_id
            )
            if len(children) != len(rows) or any(child.chunk_role != "child" for child in children):
                raise _IngestionFailure(
                    "migration_manifest_mismatch", "Source chunks are incomplete."
                )
            parent_ids = list(
                {child.parent_chunk_id for child in children if child.parent_chunk_id}
            )
            parents = self.chunks.list_by_ids(parent_ids, index_version=source_id)
            if len(parents) != len(parent_ids) or any(
                parent.chunk_role != "parent" for parent in parents
            ):
                raise _IngestionFailure(
                    "migration_manifest_mismatch", "Source parents are incomplete."
                )
            parent_ordinals = {parent.chunk_id: parent.ordinal for parent in parents}
            self.chunks.add_many(
                [clone(parent) for parent in parents if parent.chunk_id not in copied_parents]
            )
            copied_parents.update(parent_ids)
            cloned = [clone(child) for child in children]
            self.chunks.add_many(cloned)
            vectors = {str(row["chunk_id"]): list(row["embedding"]) for row in rows}
            records = []
            for child, new in zip(children, cloned, strict=True):
                if child.document_id != job.document_id or child.chunk_id in seen_children:
                    raise _IngestionFailure(
                        "migration_manifest_mismatch", "Source identities are invalid."
                    )
                seen_children.add(child.chunk_id)
                vector = vectors[child.chunk_id]
                if len(vector) != target.embedding_dimension:
                    raise _IngestionFailure(
                        "migration_profile_mismatch", "Source vectors are invalid."
                    )
                records.append(
                    VectorRecord(
                        new.chunk_id,
                        new.document_id,
                        new.index_version,
                        new.parent_chunk_id,
                        new.content,
                        new.label,
                        new.location,
                        vector,
                    )
                )
            self.vectors.upsert(collection=collection, records=records)
        self._verify_vectors(
            collection=collection, index_version=job.index_version, expected=source.chunk_count
        )
        if (
            self.chunks.count_for_version(job.index_version, chunk_role="child")
            != source.chunk_count
        ):
            raise _IngestionFailure(
                "migration_manifest_mismatch", "Migrated chunks are incomplete."
            )
        current = self.documents.get(job.document_id)
        if (
            not self.jobs.still_owned(job.ingestion_job_id, owner_token=owner_token)
            or current is None
            or current.active_index_version != source_id
        ):
            raise ExecutionOwnershipLost(job.ingestion_job_id)
        self.versions.mark_validated(job.index_version, chunk_count=source.chunk_count)
        if not self.registry.promote(job.index_version):
            raise _IngestionFailure("document_not_available", "Document is unavailable.")
        return source.chunk_count

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
            # 文档终态（ADR-040 决策 3）：只有没有可用版本的文档才降级，
            # 因此失败的重建索引不会影响仍在服务旧版本的文档。
            self.documents.mark_failed(job.document_id)

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

    def _extract_text(self, job: IngestionJobRecord, *, owner_token: str) -> _Extraction:
        """优先用受控存储中的原文件解析；历史数据退回首次摄取保存的归一化文本。

        原文件在 P1 起被持久化，因此「解析失败的 Job 重试」不再因为没有原始字节而必然
        失败；只有早于受控存储的旧文档才会走到兜底路径。

        文本长度是**显式预算**（ADR-036 决策 2）：超过 ``max_extracted_chars`` 时明确失败，
        绝不写入被截断的前缀冒充全文。

        MinerU 档位下的文件走**分片编排**（ADR-037）：PDF 按页数与字节双限切片，其它远端
        类型是单片 manifest；每一片都在 manifest 里留下页范围、字节、hash、provider batch
        id 与终态，因此可以复用成功片、只重试失败片、并在超时后续轮询同一 provider 任务。
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
            # 历史兜底路径没有分片事实：按「单片、无页码」记录，绝不伪造页数。
            return _Extraction(text=stored, shard_count=0, page_count=0, sharded=False)

        remote = self._mineru_parser_for(filename=filename, content_type=content_type)
        if remote is not None:
            return self._extract_text_sharded(
                job,
                original,
                filename=filename,
                remote=remote,
                owner_token=owner_token,
            )
        return self._extract_text_locally(
            job, original, filename=filename, content_type=content_type
        )

    def _extract_text_locally(
        self, job: IngestionJobRecord, original: bytes, *, filename: str, content_type: str
    ) -> _Extraction:
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
        return _Extraction(text=text, shard_count=0, page_count=parsed.pages, sharded=False)

    # MinerU 分片编排（ADR-037）----------------------------------------------------

    def _mineru_parser_for(self, *, filename: str, content_type: str) -> MinerUParser | None:
        """返回真正解析该文件的 MinerU adapter；本地路由时返回 ``None``。

        不新增抽象：直接读取装配结果（``RoutedParser.remote`` 或本身就是 MinerU）。
        """

        if self._effective_parser_provider(filename=filename, content_type=content_type) != (
            MinerUParser.provider_name
        ):
            return None
        return self._mineru_parser_for_shard()

    def _extract_text_sharded(
        self,
        job: IngestionJobRecord,
        original: bytes,
        *,
        filename: str,
        remote: MinerUParser,
        owner_token: str,
    ) -> _Extraction:
        provider = remote.provider_name
        # parser_options 参与复用键：模型版本变了，解析结果就不再等价。
        options = remote.model_version
        is_pdf = filename.casefold().endswith(".pdf")

        page_count = 0
        if is_pdf:
            try:
                page_count = count_pages(original)
            except ParserError as error:
                raise _IngestionFailure(str(error), _parser_failure_message(str(error))) from error
            try:
                ranges: list[PageRange | None] = list(
                    plan_shards(
                        page_count,
                        len(original),
                        max_pages=self.settings.mineru_max_pages,
                        max_bytes=remote.max_bytes,
                    )
                )
            except ParserError as error:
                raise _IngestionFailure(str(error), _parser_failure_message(str(error))) from error
        else:
            # 非 PDF 远端文件：单片 manifest（无页范围），仍然满足闭合校验。
            ranges = [None]

        shard_payloads = self._slice_shards(original, ranges=ranges, remote=remote)

        texts: list[str] = []
        blocks: list[DocumentBlock] = []
        warnings: list[str] = []
        for index, (page_range, payload) in enumerate(shard_payloads):
            shard_text, shard_blocks, shard_warnings = self._extract_shard(
                job=job,
                shard_index=index,
                page_range=page_range,
                payload=payload,
                filename=filename,
                provider=provider,
                options=options,
                owner_token=owner_token,
            )
            texts.append(shard_text)
            warnings.extend(shard_warnings)
            # 页码绝对化：分片内第 0 页对应 page_start-1，无页范围（非 PDF）时为 None，
            # 因此块不带页码 —— 绝不伪造第 1 页（ADR-038 决策 2）。
            offset = page_range.first_page - 1 if page_range is not None else None
            blocks.extend(with_page_offset(shard_blocks, page_offset=offset))

        # 文档文本与索引内容必须来自**同一个来源**（ADR-038）：有结构化块时用块拼出文档文本，
        # 否则退回规范化后的 Markdown。否则「存起来的文本」与「真正被索引的内容」会是两份
        # 不同的表示（例如 Markdown 里的图片引用不会进入块），完整性校验也就失去意义。
        merged = (
            "\n\n".join(block.text.strip() for block in blocks if block.text.strip())
            if blocks
            else normalize_text("\n\n".join(texts))
        )
        if not merged.strip():
            raise _IngestionFailure("no_extractable_text", "No indexable content was produced.")
        if len(merged) > self.settings.max_extracted_chars:
            raise _IngestionFailure(
                "extracted_text_too_large",
                "The extracted text was not indexed because it exceeded the budget.",
            )
        self.contents.upsert(
            document_id=job.document_id,
            extracted_text=merged,
            parser_provider=provider,
            truncated=False,
        )
        return _Extraction(
            text=merged,
            shard_count=len(shard_payloads),
            page_count=page_count,
            sharded=True,
            blocks=tuple(blocks),
            warnings=tuple(warnings),
        )

    def _slice_shards(
        self, original: bytes, *, ranges: list[PageRange | None], remote: MinerUParser
    ) -> list[tuple[PageRange | None, bytes]]:
        """按计划切片并逐片执行**真实字节**上限检查（计划只是估算）。"""

        payloads: list[tuple[PageRange | None, bytes]] = []
        for page_range in ranges:
            if page_range is None:
                payload = original
            else:
                try:
                    payload = slice_pages(original, page_range.first_page, page_range.last_page)
                except ParserError as error:
                    raise _IngestionFailure(
                        str(error), _parser_failure_message(str(error))
                    ) from error
            if len(payload) > remote.max_bytes:
                # 单页仍超 provider 字节硬限：明确失败，绝不无限递归拆分。
                raise _IngestionFailure(
                    "shard_too_large",
                    "A single page exceeds the parser's per-file byte limit.",
                )
            payloads.append((page_range, payload))
        return payloads

    def _extract_shard(
        self,
        *,
        job: IngestionJobRecord,
        shard_index: int,
        page_range: PageRange | None,
        payload: bytes,
        filename: str,
        provider: str,
        options: str,
        owner_token: str,
    ) -> tuple[str, tuple[DocumentBlock, ...], tuple[str, ...]]:
        """执行（或复用 / 续轮询）一个分片，返回该片的文本、结构化块与安全警告。"""

        digest = hashlib.sha256(payload).hexdigest()
        existing = self.shards.get_by_index(
            index_version=job.index_version, shard_index=shard_index, with_text=True
        )
        if existing is not None and existing.status == SHARD_STATUS_SUCCESS:
            # 同一 index version 内已成功（上一轮 attempt）：直接复用，不再调用 provider。
            return (
                existing.extracted_text or "",
                deserialize_blocks(existing.blocks_json),
                (),
            )

        if existing is None:
            reusable = self.shards.find_reusable(
                sha256=digest, parser_provider=provider, parser_options=options
            )
            if reusable is not None and reusable.extracted_text is not None:
                record = self.shards.create(
                    ingestion_job_id=job.ingestion_job_id,
                    document_id=job.document_id,
                    index_version=job.index_version,
                    shard_index=shard_index,
                    byte_size=len(payload),
                    sha256=digest,
                    parser_provider=provider,
                    parser_options=options,
                    page_start=page_range.first_page if page_range else None,
                    page_end=page_range.last_page if page_range else None,
                )
                # 复用键命中：不调用 provider，直接复制文本与结构（成功分片不重复付费解析）。
                self.shards.mark_success(
                    record.shard_id,
                    extracted_text=reusable.extracted_text,
                    reused=True,
                    blocks_json=reusable.blocks_json,
                )
                return (
                    reusable.extracted_text,
                    deserialize_blocks(reusable.blocks_json),
                    (),
                )
            resumable = self.shards.find_resumable(
                sha256=digest, parser_provider=provider, parser_options=options
            )
            record = self.shards.create(
                ingestion_job_id=job.ingestion_job_id,
                document_id=job.document_id,
                index_version=job.index_version,
                shard_index=shard_index,
                byte_size=len(payload),
                sha256=digest,
                parser_provider=provider,
                parser_options=options,
                page_start=page_range.first_page if page_range else None,
                page_end=page_range.last_page if page_range else None,
                # 续轮询凭据：上一轮超时留下的 batch id（超时 ≠ 任务未创建）。
                provider_batch_id=resumable.provider_batch_id if resumable else None,
            )
        else:
            # 同一 index version 内已有非 success 的分片（例如同一执行内的再次尝试）：
            # 原地推进它的 attempt，不新建行（UNIQUE(index_version, shard_index) 不允许）。
            record = existing

        if record.status == SHARD_STATUS_PENDING and not self.shards.claim(record.shard_id):
            raise _IngestionFailure(
                "shard_state_conflict", "A shard could not be claimed for execution."
            )

        batch_id = record.provider_batch_id
        if not batch_id:
            remote = self._mineru_parser_for_shard()
            if remote is None:
                raise _IngestionFailure(
                    "parser_unavailable", "The document parser is unavailable for this request."
                )
            try:
                task = remote.submit(
                    filename=_shard_filename(filename, page_range),
                    content_type="application/pdf",
                    data=payload,
                )
            except ParserError as error:
                self.shards.mark_failed(record.shard_id, failure_code=str(error))
                raise _IngestionFailure(str(error), _parser_failure_message(str(error))) from error
            batch_id = task.batch_id
            # 立刻持久化：即使随后轮询超时，重试也能续轮询同一个 provider 任务。
            self.shards.set_provider_batch_id(record.shard_id, provider_batch_id=batch_id)

        remote = self._mineru_parser_for_shard()
        if remote is None:
            raise _IngestionFailure(
                "parser_unavailable", "The document parser is unavailable for this request."
            )
        deadline = time.monotonic() + self.settings.parser_timeout_seconds
        try:
            parsed: ParsedDocument = remote.fetch(
                batch_id, deadline=deadline, title=_shard_filename(filename, page_range)
            )
        except ParserError as error:
            code = str(error)
            if code == "parse_timeout":
                # 保留 running + batch id：Job 明确失败，但重试先续轮询同一 provider 任务。
                raise _IngestionFailure(
                    "parse_timeout",
                    "The parser did not finish within the bounded wait; "
                    "the task is kept for resumption.",
                ) from error
            self.shards.mark_failed(record.shard_id, failure_code=code)
            raise _IngestionFailure(code, _parser_failure_message(code)) from error
        # 结构化块随分片一起持久化：复用分片时必须能取回同样的结构，否则会出现
        # 「首次结构化、重建索引回退文本」的行为不一致（ADR-038 决策 1）。
        blocks_json = serialize_blocks(parsed.blocks)
        warnings = list(parsed.warnings)
        blocks: tuple[DocumentBlock, ...] = parsed.blocks
        if parsed.blocks and blocks_json is None:
            warnings.append("structure_not_persisted")
            blocks = ()
        self.shards.mark_success(
            record.shard_id, extracted_text=parsed.text, blocks_json=blocks_json
        )
        return parsed.text, blocks, tuple(warnings)

    def _mineru_parser_for_shard(self) -> MinerUParser | None:
        candidate = getattr(self.parser, "remote", None)
        if isinstance(candidate, MinerUParser):
            return candidate
        if isinstance(self.parser, MinerUParser):
            return self.parser
        return None

    def _verify_shards(
        self, *, index_version: str, expected_shard_count: int, expected_pages: int
    ) -> None:
        """分片闭合校验（ADR-037）：无缺片、无重叠、页范围连续、全部 success。

        只有全部通过才允许 promote；缺片或仍有 ``running`` 的分片一律明确失败，
        绝不用「部分成功」冒充全文索引。
        """

        rows = self.shards.list_for_version(index_version)
        if len(rows) != expected_shard_count:
            raise _IngestionFailure(
                "shard_manifest_mismatch", "The shard manifest did not match the planned shards."
            )
        for position, row in enumerate(rows):
            if row.shard_index != position:
                raise _IngestionFailure(
                    "shard_manifest_mismatch", "The shard manifest had a gap or duplicate."
                )
            if row.status != SHARD_STATUS_SUCCESS:
                raise _IngestionFailure(
                    "shard_incomplete", "Not every shard of the document finished successfully."
                )
        previous_end = 0
        for row in rows:
            if row.page_start is None:
                continue
            if row.page_start != previous_end + 1:
                raise _IngestionFailure(
                    "shard_range_gap", "The shard page ranges were not contiguous."
                )
            previous_end = row.page_end or row.page_start
        if expected_pages and previous_end != expected_pages:
            raise _IngestionFailure(
                "shard_range_gap", "The shard page ranges did not cover the whole document."
            )

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

    def _index_text(
        self, job: IngestionJobRecord, extraction: _Extraction, *, owner_token: str
    ) -> int:
        text = extraction.text
        # 有 provider 结构化块时按结构 + token 预算分块（ADR-038）；否则回退到文本分块，
        # 并记录回退码 —— 绝不静默把「没有结构」当成「有结构」。
        if extraction.blocks:
            chunked = chunk_blocks(
                extraction.blocks,
                parent_tokens=self.settings.parent_chunk_tokens,
                child_tokens=self.settings.chunk_tokens,
                overlap_tokens=self.settings.chunk_overlap_tokens,
                max_section_depth=self.settings.max_section_depth,
            )
        else:
            logger.info(
                "ingestion_structured_chunking_fallback warnings=%s",
                ",".join(extraction.warnings) or "none",
            )
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
        labels = {parent.ordinal: _label_for(parent) for parent in chunked.parents}
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
                location=_page_location(parent.page_start, parent.page_end, parent.ordinal),
                label=labels[parent.ordinal],
                section_path=parent.section_path,
                block_type=parent.block_type,
                page_start=parent.page_start,
                page_end=parent.page_end,
                content_hash=content_hash(parent.content),
                token_count=parent.token_count or estimate_tokens(parent.content),
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
                # 定位串带上**真实**页范围（没有可靠页码时退回 chunk-N，绝不伪造页码）。
                location=_page_location(child.page_start, child.page_end, child.ordinal),
                label=labels[child.parent_ordinal],
                section_path=child.section_path,
                block_type=child.block_type,
                page_start=child.page_start,
                page_end=child.page_end,
                content_hash=content_hash(child.content),
                token_count=child.token_count or estimate_tokens(child.content),
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
                expected_text_length=len(text),
            )
            if extraction.sharded:
                # 分片闭合（无缺片/无重叠/页范围连续/全部 success）才允许 promote。
                self._verify_shards(
                    index_version=index_version,
                    expected_shard_count=extraction.shard_count,
                    expected_pages=extraction.page_count,
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
        expected_text_length: int,
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
        if len(stored_text) != expected_text_length:
            # 合并文本必须与已存文本一致：长度不符说明有分片内容没有进入最终文本。
            raise _IngestionFailure(
                "content_length_mismatch", "The stored text did not match the parsed content."
            )

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
                self.shards.delete_for_index_version(version.index_version)
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
    # ADR-037：PDF 分片与超时语义（与「提交失败」明确区分）。
    "pdf_encrypted": "The PDF is encrypted and cannot be parsed.",
    "pdf_damaged": "The PDF is damaged and could not be read.",
    "shard_too_large": "A single page exceeds the parser's per-file limit.",
    "parse_timeout": (
        "The parser did not finish within the bounded wait; the task is kept for resumption."
    ),
    "shard_incomplete": "Not every shard of the document finished successfully.",
    "shard_manifest_mismatch": "The shard manifest did not match the planned shards.",
    "shard_range_gap": "The shard page ranges did not cover the whole document.",
    "shard_state_conflict": "A shard could not be claimed for execution.",
    "content_length_mismatch": "The stored text did not match the parsed content.",
}


def _parser_failure_message(code: str) -> str:
    return _PARSER_FAILURE_MESSAGES.get(code, "The document could not be parsed.")


def _seconds_ago(seconds: int):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    return utc_now() - timedelta(seconds=seconds)


def _shard_filename(filename: str, page_range: PageRange | None) -> str:
    """给分片一个稳定且可辨识的 provider 文件名（不含路径，保留原扩展名）。"""

    if page_range is None:
        return filename
    stem, separator, suffix = filename.rpartition(".")
    label = f"p{page_range.first_page}-{page_range.last_page}"
    if not separator:
        return f"{filename}.{label}"
    return f"{stem}.{label}.{suffix}"


def _label_for(parent) -> str:
    """Parent 的展示标签：优先**真实**章节路径，否则退回首行。"""

    section_path = getattr(parent, "section_path", None)
    if section_path:
        return str(section_path)[:200]
    block = parent.content
    if block.strip():
        return block.strip().splitlines()[0][:200]
    return f"Section {parent.ordinal + 1}"


def _page_location(page_start: int | None, page_end: int | None, ordinal: int) -> str:
    """人类可读的定位串：有真实页码时用页范围，否则退回 chunk 序号（绝不伪造页码）。"""

    if page_start is None:
        return f"chunk-{ordinal}"
    if page_end is not None and page_end != page_start:
        return f"p{page_start}-{page_end}#c{ordinal}"
    return f"p{page_start}#c{ordinal}"


__all__ = ["IngestionService", "UploadOutcome"]
