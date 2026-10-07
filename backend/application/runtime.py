"""运行时组合根（composition root）。

它为 API 与 workers 装配已批准的 adapters、repositories 与 services。
它不创建任何新抽象，也不持有任何属于 Graph State 的内容。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from backend.application.index_registry import IndexRegistry
from backend.application.ingestion import IngestionService
from backend.infrastructure.config import Settings
from backend.infrastructure.database import (
    MySQLConnectionPool,
    PoolSettings,
    ReadOnlyQueryRunner,
)
from backend.infrastructure.embedding import EmbeddingProvider, create_embedding_provider
from backend.infrastructure.parsers import DocumentParser, create_parser
from backend.infrastructure.repositories.approvals import ApprovalRepository
from backend.infrastructure.repositories.auth import AuthRepository
from backend.infrastructure.repositories.conversation import (
    ConversationRepository,
    RunEventRepository,
    RunRepository,
    ThreadRepository,
)
from backend.infrastructure.repositories.documents import (
    ChunkRepository,
    DocumentContentRepository,
    DocumentRepository,
    IndexVersionRepository,
    IngestionJobRepository,
)
from backend.infrastructure.repositories.structured import (
    ExportArtifactRepository,
    QueryResultRepository,
)
from backend.infrastructure.schema import initialize_read_only_account, initialize_schema
from backend.infrastructure.vector_store import VectorStore, create_vector_store
from backend.tools.sql_validator import ProjectSQLValidator, build_validator

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    application_pool: MySQLConnectionPool
    read_only_pool: MySQLConnectionPool
    auth: AuthRepository
    conversations: ConversationRepository
    threads: ThreadRepository
    runs: RunRepository
    run_events: RunEventRepository
    approvals: ApprovalRepository
    documents: DocumentRepository
    document_contents: DocumentContentRepository
    versions: IndexVersionRepository
    chunks: ChunkRepository
    jobs: IngestionJobRepository
    query_results: QueryResultRepository
    artifacts: ExportArtifactRepository
    validator: ProjectSQLValidator
    query_runner: ReadOnlyQueryRunner
    embeddings: EmbeddingProvider
    vectors: VectorStore
    parser: DocumentParser
    registry: IndexRegistry
    ingestion: IngestionService
    _worker_slots: threading.BoundedSemaphore

    def try_claim_worker_slot(self) -> bool:
        # worker 容量有界（由 BoundedSemaphore 限制），用于约束并发的摄取任务数量。
        return self._worker_slots.acquire(blocking=False)

    def release_worker_slot(self) -> None:
        try:
            self._worker_slots.release()
        except ValueError:  # pragma: no cover - defensive release
            pass

    def close(self) -> None:
        self.application_pool.close()
        self.read_only_pool.close()
        close = getattr(self.vectors, "close", None)
        if callable(close):
            close()


def build_runtime(settings: Settings, *, initialize: bool = True) -> Runtime:
    application_pool = MySQLConnectionPool(PoolSettings.application(settings))
    read_only_pool = MySQLConnectionPool(PoolSettings.read_only(settings))

    if initialize:
        initialize_schema(settings)
        root_password = settings.mysql_root_password.get_secret_value()
        if settings.mysql_readonly_password.get_secret_value():
            initialize_read_only_account(settings, root_password=root_password)

    auth = AuthRepository(application_pool)
    conversations = ConversationRepository(application_pool)
    threads = ThreadRepository(application_pool)
    runs = RunRepository(application_pool)
    run_events = RunEventRepository(application_pool)
    approvals = ApprovalRepository(application_pool)
    documents = DocumentRepository(application_pool)
    document_contents = DocumentContentRepository(application_pool)
    versions = IndexVersionRepository(application_pool)
    chunks = ChunkRepository(application_pool)
    jobs = IngestionJobRepository(application_pool)
    query_results = QueryResultRepository(application_pool)
    artifacts = ExportArtifactRepository(application_pool)

    validator = build_validator(settings)
    query_runner = ReadOnlyQueryRunner(
        read_only_pool,
        timeout_seconds=settings.sql_query_timeout_seconds,
        max_result_bytes=settings.sql_max_result_bytes,
    )
    embeddings = create_embedding_provider(settings)
    vectors = create_vector_store(settings)
    parser = create_parser(settings)
    registry = IndexRegistry(documents=documents, versions=versions, chunks=chunks)
    ingestion = IngestionService(
        settings=settings,
        documents=documents,
        contents=document_contents,
        versions=versions,
        chunks=chunks,
        jobs=jobs,
        registry=registry,
        parser=parser,
        embeddings=embeddings,
        vectors=vectors,
    )
    return Runtime(
        settings=settings,
        application_pool=application_pool,
        read_only_pool=read_only_pool,
        auth=auth,
        conversations=conversations,
        threads=threads,
        runs=runs,
        run_events=run_events,
        approvals=approvals,
        documents=documents,
        document_contents=document_contents,
        versions=versions,
        chunks=chunks,
        jobs=jobs,
        query_results=query_results,
        artifacts=artifacts,
        validator=validator,
        query_runner=query_runner,
        embeddings=embeddings,
        vectors=vectors,
        parser=parser,
        registry=registry,
        ingestion=ingestion,
        _worker_slots=threading.BoundedSemaphore(max(1, settings.mysql_pool_size)),
    )
