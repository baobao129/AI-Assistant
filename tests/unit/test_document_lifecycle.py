"""Document 生命周期（ADR-040 / CC-API-003）的离线单测。

覆盖 G19/G20 的两个根因：

- G19：文档**上传即 available**，与「是否真的有可用 active 版本」脱钩；
- G20：重复上传**固定返回 success**，坏文档因此无法通过 API 自愈。

用记录型连接池桩验证仓储层的 SQL 语义，用内存替身验证 ``IngestionService`` 的状态上报。
不触网、不连库。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.application.ingestion import IngestionService, UploadOutcome
from backend.domain.errors import AppError
from backend.infrastructure.config import Settings
from backend.infrastructure.repositories.documents import (
    DOCUMENT_STATUS_AVAILABLE,
    DOCUMENT_STATUS_DELETED,
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_INDEXING,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCESS,
    DocumentRecord,
    DocumentRepository,
    IngestionJobRecord,
)

NOW = datetime(2026, 10, 10, 12, 0, 0)


# --- 记录型连接池 ----------------------------------------------------------


class RecordingPool:
    """记录 execute/query_one 的连接池桩，可注入受影响行数与查询结果。"""

    def __init__(self, *, affected: int = 1, row: tuple | None = None) -> None:
        self.affected = affected
        self.row = row
        self.executions: list[tuple[str, tuple]] = []
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql: str, parameters=()) -> int:
        self.executions.append((sql, tuple(parameters)))
        return self.affected

    def query_one(self, sql: str, parameters=()):
        self.queries.append((sql, tuple(parameters)))
        return self.row

    def query(self, sql: str, parameters=()):
        self.queries.append((sql, tuple(parameters)))
        return []


# --- 仓储层：状态词汇与受守卫的转换 ----------------------------------------


def test_new_documents_start_as_indexing_not_available():
    # G19 根因：上传即 available。新文档必须从 indexing 开始。
    pool = RecordingPool()
    record = DocumentRepository(pool).create(
        user_id="user-1",
        title="t",
        source_name="s.md",
        content_type="text/markdown",
        content_fingerprint="f" * 64,
        byte_size=10,
    )
    assert record.status == DOCUMENT_STATUS_INDEXING
    _, parameters = pool.executions[0]
    assert DOCUMENT_STATUS_INDEXING in parameters
    assert DOCUMENT_STATUS_AVAILABLE not in parameters


def test_activate_sets_status_and_version_in_one_guarded_update():
    # 状态与 active 版本必须写在同一条条件 UPDATE 里，且 deleted 永不被激活。
    pool = RecordingPool(affected=1)
    activated = DocumentRepository(pool).activate("doc-1", "v1")

    assert activated is True
    sql, parameters = pool.executions[0]
    assert "status = %s" in sql and "active_index_version = %s" in sql
    assert "status <> %s" in sql  # deleted 守卫
    assert parameters[0] == DOCUMENT_STATUS_AVAILABLE
    assert parameters[1] == "v1"
    assert parameters[-1] == DOCUMENT_STATUS_DELETED


def test_activate_reports_false_when_the_document_is_deleted():
    # 并发删除：条件 UPDATE 命中 0 行 -> promote 无副作用地失败。
    pool = RecordingPool(affected=0)
    assert DocumentRepository(pool).activate("doc-1", "v1") is False


def test_mark_failed_is_guarded_by_the_absence_of_an_active_version():
    # 失败的重建索引绝不能降级一个仍在服务旧版本的文档。
    pool = RecordingPool(affected=1)
    failed = DocumentRepository(pool).mark_failed("doc-1")

    assert failed is True
    sql, parameters = pool.executions[0]
    assert "active_index_version IS NULL" in sql
    assert parameters[0] == DOCUMENT_STATUS_FAILED
    assert parameters[-1] == DOCUMENT_STATUS_DELETED


def test_mark_failed_reports_false_when_an_active_version_exists():
    pool = RecordingPool(affected=0)
    assert DocumentRepository(pool).mark_failed("doc-1") is False


# --- 摄取层：重复上传的状态上报 --------------------------------------------


def _document(
    *, status: str = DOCUMENT_STATUS_AVAILABLE, active: str | None = "v1"
) -> DocumentRecord:
    return DocumentRecord(
        document_id="doc-1",
        user_id="user-1",
        title="t",
        source_name="s.md",
        content_type="text/markdown",
        content_fingerprint="f" * 64,
        byte_size=10,
        status=status,
        active_index_version=active,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )


def _job(status: str, *, index_version: str = "v1") -> IngestionJobRecord:
    return IngestionJobRecord(
        ingestion_job_id="job-1",
        document_id="doc-1",
        index_version=index_version,
        job_type="ingest",
        status=status,
        attempt=1,
        failure_code=None,
        failure_message=None,
        claimed_at=None,
        created_at=NOW,
        updated_at=NOW,
        finished_at=None,
    )


class _FakeDocuments:
    def __init__(self, document: DocumentRecord | None) -> None:
        self.document = document
        self.marked_deleted: list[str] = []

    def find_by_fingerprint(self, *, user_id: str, fingerprint: str):
        return self.document

    def get(self, document_id: str, *, user_id: str | None = None):
        return self.document

    def mark_deleted(self, document_id: str, *, user_id: str) -> bool:
        self.marked_deleted.append(document_id)
        return True


class _FakeJobs:
    def __init__(self, jobs: list[IngestionJobRecord] | None = None) -> None:
        self._jobs = list(jobs or [])
        self.created: list[dict] = []

    def list_for_document(self, document_id: str):
        return list(self._jobs)

    def find_by_idempotency_key(self, *, document_id: str, idempotency_key: str):
        return None

    def max_attempt(self, document_id: str) -> int:
        return len(self._jobs)

    def create(self, **fields):
        self.created.append(fields)
        return _job(JOB_STATUS_SUCCESS, index_version=fields["index_version"])


class _FakeFiles:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    def get(self, document_id: str):
        return self.rows.get(document_id)

    def upsert(self, **fields):
        self.rows[fields["document_id"]] = fields


class _FakeContents:
    def __init__(self, stored: str | None = "text") -> None:
        self.stored = stored

    def get(self, document_id: str):
        return self.stored


class _FakeVersions:
    def create(self, **fields):
        from backend.infrastructure.repositories.documents import (
            INDEX_STATUS_INDEXING,
            IndexVersionRecord,
        )

        return IndexVersionRecord(
            index_version="v2",
            document_id=fields["document_id"],
            status=INDEX_STATUS_INDEXING,
            chunk_count=0,
            embedding_provider=fields["embedding_provider"],
            embedding_model=fields["embedding_model"],
            embedding_dimension=fields["embedding_dimension"],
            collection_name=fields["collection_name"],
            parser_provider=fields["parser_provider"],
            failure_code=None,
            created_at=NOW,
            updated_at=NOW,
            retired_at=None,
        )


class _FakeEmbeddings:
    provider_name = "deterministic"
    model = "test-model"
    dimension = 4


class _FakeParser:
    provider_name = "lightweight"


def _service(
    *,
    document: DocumentRecord | None,
    jobs: list[IngestionJobRecord] | None = None,
    contents: str | None = "text",
):
    service = object.__new__(IngestionService)
    service.settings = Settings(_env_file=None)
    service.documents = _FakeDocuments(document)
    service.jobs = _FakeJobs(jobs)
    service.files = _FakeFiles()
    service.contents = _FakeContents(contents)
    service.versions = _FakeVersions()
    service.embeddings = _FakeEmbeddings()
    service.parser = _FakeParser()
    return service


def _stored(byte_size: int = 10):
    from backend.infrastructure.file_storage import StoredUpload

    return StoredUpload(storage_key="uploads/x", sha256="f" * 64, byte_size=byte_size)


def test_duplicate_upload_reports_the_latest_job_status_not_a_hardcoded_success():
    # G20 根因：固定返回 success。现在必须报告真实 Job 状态。
    for status in (JOB_STATUS_RUNNING, JOB_STATUS_FAILED, "pending"):
        service = _service(document=_document(), jobs=[_job(status)])
        outcome = service.submit_upload(
            user_id="user-1",
            filename="s.md",
            content_type="text/markdown",
            stored=_stored(),
        )
        assert outcome.status == status, status
        assert outcome.duplicate is True
        assert outcome.ingestion_job_id == "job-1"
        assert service.jobs.created == []  # 重复上传不新建 Job


def test_duplicate_upload_without_job_history_reports_available_only_when_usable():
    # 没有 Job 历史行时按 active 版本判断，而不是一律 success。
    usable = _service(document=_document(status=DOCUMENT_STATUS_AVAILABLE, active="v1"))
    outcome = usable.submit_upload(
        user_id="user-1", filename="s.md", content_type="text/markdown", stored=_stored()
    )
    assert outcome.status == JOB_STATUS_SUCCESS
    assert outcome.duplicate is True

    # 正是 P0 遗留坏文档的形态：available 但没有 active 版本 -> 不得报 success。
    broken = _service(document=_document(status=DOCUMENT_STATUS_INDEXING, active=None))
    outcome = broken.submit_upload(
        user_id="user-1", filename="s.md", content_type="text/markdown", stored=_stored()
    )
    assert outcome.status == JOB_STATUS_FAILED


def test_delete_accepts_any_non_deleted_status_so_a_broken_document_can_be_removed():
    # ADR-040 决策 5：卡住/坏掉的文档必须能被清掉，否则用户没有自愈路径。
    for status in (DOCUMENT_STATUS_INDEXING, DOCUMENT_STATUS_FAILED, DOCUMENT_STATUS_AVAILABLE):
        service = _service(document=_document(status=status, active=None))
        outcome = service.submit_delete(user_id="user-1", document_id="doc-1")
        assert isinstance(outcome, UploadOutcome)
        assert service.documents.marked_deleted == ["doc-1"]


def test_delete_refuses_a_document_that_is_already_deleted():
    service = _service(document=_document(status=DOCUMENT_STATUS_DELETED, active=None))
    with pytest.raises(AppError) as error:
        service.submit_delete(user_id="user-1", document_id="doc-1")
    assert error.value.code == "not_found"


def test_reindex_distinguishes_not_ready_from_not_found():
    # indexing/failed 与「不存在」是两回事，必须给出不同的解释。
    for status in (DOCUMENT_STATUS_INDEXING, DOCUMENT_STATUS_FAILED):
        service = _service(document=_document(status=status, active=None))
        with pytest.raises(AppError) as error:
            service.submit_reindex(user_id="user-1", document_id="doc-1")
        assert error.value.code == "conflict", status
        assert "ready" in error.value.message

    missing = _service(document=None)
    with pytest.raises(AppError) as error:
        missing.submit_reindex(user_id="user-1", document_id="doc-1")
    assert error.value.code == "not_found"


def test_reindex_requires_stored_content_for_an_available_document():
    service = _service(document=_document(), contents=None)
    with pytest.raises(AppError) as error:
        service.submit_reindex(user_id="user-1", document_id="doc-1")
    assert error.value.code == "conflict"
    assert "content" in error.value.message
