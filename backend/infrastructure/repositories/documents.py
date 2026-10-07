"""Document, index-version, chunk, and ingestion-job persistence (ADR-019)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool

INDEX_STATUS_ACTIVE = "active"
INDEX_STATUS_INDEXING = "indexing"
INDEX_STATUS_RETIRED = "retired"
INDEX_STATUS_FAILED = "failed"

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCESS = "success"
JOB_STATUS_FAILED = "failed"

DOCUMENT_STATUS_AVAILABLE = "available"
DOCUMENT_STATUS_DELETED = "deleted"

DOCUMENT_COLUMNS = (
    "document_id, user_id, title, source_name, content_type, content_fingerprint, byte_size, "
    "status, active_index_version, created_at, updated_at, deleted_at"
)
INDEX_VERSION_COLUMNS = (
    "index_version, document_id, status, chunk_count, embedding_provider, embedding_model, "
    "embedding_dimension, collection_name, parser_provider, failure_code, created_at, updated_at, "
    "retired_at"
)
JOB_COLUMNS = (
    "ingestion_job_id, document_id, index_version, job_type, status, attempt, failure_code, "
    "failure_message, claimed_at, created_at, updated_at, finished_at"
)


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    user_id: str
    title: str
    source_name: str
    content_type: str
    content_fingerprint: str
    byte_size: int
    status: str
    active_index_version: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class IndexVersionRecord:
    index_version: str
    document_id: str
    status: str
    chunk_count: int
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    collection_name: str
    parser_provider: str
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    retired_at: datetime | None


@dataclass(frozen=True)
class IngestionJobRecord:
    ingestion_job_id: str
    document_id: str
    index_version: str
    job_type: str
    status: str
    attempt: int
    failure_code: str | None
    failure_message: str | None
    claimed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    document_id: str
    index_version: str
    parent_chunk_id: str | None
    chunk_role: str
    ordinal: int
    content: str
    location: str | None
    label: str


class DocumentRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        user_id: str,
        title: str,
        source_name: str,
        content_type: str,
        content_fingerprint: str,
        byte_size: int,
    ) -> DocumentRecord:
        document_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO documents (document_id, user_id, title, source_name, content_type, "
            "content_fingerprint, byte_size, status, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                document_id,
                user_id,
                title[:300],
                source_name[:300],
                content_type[:100],
                content_fingerprint,
                byte_size,
                DOCUMENT_STATUS_AVAILABLE,
                now,
                now,
            ),
        )
        return DocumentRecord(
            document_id,
            user_id,
            title[:300],
            source_name[:300],
            content_type[:100],
            content_fingerprint,
            byte_size,
            DOCUMENT_STATUS_AVAILABLE,
            None,
            now,
            now,
            None,
        )

    def get(self, document_id: str, *, user_id: str | None = None) -> DocumentRecord | None:
        parameters: list[Any] = [document_id]
        clause = ""
        if user_id is not None:
            clause = "AND user_id = %s"
            parameters.append(user_id)
        row = self.pool.query_one(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE document_id = %s {clause}",
            parameters,
        )
        return _document(row)

    def list(
        self, *, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[DocumentRecord], str | None]:
        parameters: list[Any] = [user_id]
        clause = ""
        if cursor:
            clause = "AND created_at < FROM_UNIXTIME(%s / 1000000)"
            parameters.append(cursor)
        parameters.append(limit + 1)
        rows = self.pool.query(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents "
            f"WHERE user_id = %s AND status <> 'deleted' {clause} "
            "ORDER BY created_at DESC, document_id DESC LIMIT %s",
            parameters,
        )
        items = [record for row in rows[:limit] if (record := _document(row)) is not None]
        next_cursor = (
            f"{int(items[-1].created_at.timestamp() * 1_000_000)}"
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    def find_by_fingerprint(self, *, user_id: str, fingerprint: str) -> DocumentRecord | None:
        row = self.pool.query_one(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents "
            "WHERE user_id = %s AND content_fingerprint = %s AND status <> 'deleted' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, fingerprint),
        )
        return _document(row)

    def mark_deleted(self, document_id: str, *, user_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE documents SET status = 'deleted', deleted_at = %s, updated_at = %s "
            "WHERE document_id = %s AND user_id = %s AND status <> 'deleted'",
            (utc_now(), utc_now(), document_id, user_id),
        )
        return affected > 0

    def set_active_version(self, document_id: str, index_version: str | None) -> None:
        self.pool.execute(
            "UPDATE documents SET active_index_version = %s, updated_at = %s "
            "WHERE document_id = %s",
            (index_version, utc_now(), document_id),
        )

    def is_available(self, document_id: str) -> bool:
        row = self.pool.query_one(
            "SELECT status FROM documents WHERE document_id = %s", (document_id,)
        )
        return bool(row) and str(row[0]) == DOCUMENT_STATUS_AVAILABLE


class IndexVersionRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        document_id: str,
        embedding_provider: str,
        embedding_model: str,
        embedding_dimension: int,
        collection_name: str,
        parser_provider: str,
    ) -> IndexVersionRecord:
        index_version = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO document_index_versions (index_version, document_id, status, chunk_count, "
            "embedding_provider, embedding_model, embedding_dimension, collection_name, "
            "parser_provider, "
            "created_at, updated_at) VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s)",
            (
                index_version,
                document_id,
                INDEX_STATUS_INDEXING,
                embedding_provider,
                embedding_model,
                embedding_dimension,
                collection_name[:120],
                parser_provider,
                now,
                now,
            ),
        )
        return IndexVersionRecord(
            index_version=index_version,
            document_id=document_id,
            status=INDEX_STATUS_INDEXING,
            chunk_count=0,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            collection_name=collection_name[:120],
            parser_provider=parser_provider,
            failure_code=None,
            created_at=now,
            updated_at=now,
            retired_at=None,
        )

    def get(self, index_version: str) -> IndexVersionRecord | None:
        row = self.pool.query_one(
            f"SELECT {INDEX_VERSION_COLUMNS} FROM document_index_versions WHERE index_version = %s",
            (index_version,),
        )
        return _index_version(row)

    def list_for_document(self, document_id: str) -> list[IndexVersionRecord]:
        rows = self.pool.query(
            f"SELECT {INDEX_VERSION_COLUMNS} FROM document_index_versions "
            "WHERE document_id = %s ORDER BY created_at DESC",
            (document_id,),
        )
        return [record for row in rows if (record := _index_version(row)) is not None]

    def mark_validated(self, index_version: str, *, chunk_count: int) -> None:
        self.pool.execute(
            "UPDATE document_index_versions SET status = %s, chunk_count = %s, updated_at = %s "
            "WHERE index_version = %s",
            (INDEX_STATUS_ACTIVE, chunk_count, utc_now(), index_version),
        )

    def mark_failed(self, index_version: str, *, failure_code: str) -> None:
        self.pool.execute(
            "UPDATE document_index_versions SET status = %s, failure_code = %s, updated_at = %s "
            "WHERE index_version = %s AND status <> 'active'",
            (INDEX_STATUS_FAILED, failure_code[:64], utc_now(), index_version),
        )

    def retire(self, index_version: str) -> None:
        self.pool.execute(
            "UPDATE document_index_versions SET status = %s, retired_at = %s, updated_at = %s "
            "WHERE index_version = %s",
            (INDEX_STATUS_RETIRED, utc_now(), utc_now(), index_version),
        )


class IngestionJobRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        document_id: str,
        index_version: str,
        job_type: str,
        attempt: int = 1,
        idempotency_key: str | None = None,
    ) -> IngestionJobRecord:
        ingestion_job_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO ingestion_jobs (ingestion_job_id, document_id, index_version, job_type, "
            "status, attempt, idempotency_key, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                ingestion_job_id,
                document_id,
                index_version,
                job_type,
                JOB_STATUS_PENDING,
                attempt,
                idempotency_key,
                now,
                now,
            ),
        )
        return IngestionJobRecord(
            ingestion_job_id,
            document_id,
            index_version,
            job_type,
            JOB_STATUS_PENDING,
            attempt,
            None,
            None,
            None,
            now,
            now,
            None,
        )

    def get(self, ingestion_job_id: str) -> IngestionJobRecord | None:
        row = self.pool.query_one(
            f"SELECT {JOB_COLUMNS} FROM ingestion_jobs WHERE ingestion_job_id = %s",
            (ingestion_job_id,),
        )
        return _job(row)

    def find_by_idempotency_key(
        self, *, document_id: str, idempotency_key: str
    ) -> IngestionJobRecord | None:
        row = self.pool.query_one(
            f"SELECT {JOB_COLUMNS} FROM ingestion_jobs "
            "WHERE document_id = %s AND idempotency_key = %s",
            (document_id, idempotency_key),
        )
        return _job(row)

    def list_for_document(self, document_id: str) -> list[IngestionJobRecord]:
        rows = self.pool.query(
            f"SELECT {JOB_COLUMNS} FROM ingestion_jobs "
            "WHERE document_id = %s ORDER BY created_at DESC",
            (document_id,),
        )
        return [record for row in rows if (record := _job(row)) is not None]

    def claim(self, ingestion_job_id: str) -> bool:
        affected = self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, claimed_at = %s, updated_at = %s "
            "WHERE ingestion_job_id = %s AND status = %s",
            (JOB_STATUS_RUNNING, utc_now(), utc_now(), ingestion_job_id, JOB_STATUS_PENDING),
        )
        return affected > 0

    def finish(
        self,
        ingestion_job_id: str,
        *,
        status: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        now = utc_now()
        self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, failure_code = %s, failure_message = %s, "
            "updated_at = %s, finished_at = %s WHERE ingestion_job_id = %s",
            (
                status,
                failure_code[:64] if failure_code else None,
                failure_message[:500] if failure_message else None,
                now,
                now,
                ingestion_job_id,
            ),
        )

    def max_attempt(self, document_id: str) -> int:
        row = self.pool.query_one(
            "SELECT MAX(attempt) FROM ingestion_jobs WHERE document_id = %s", (document_id,)
        )
        return int(row[0]) if row and row[0] is not None else 0

    # Startup recovery ------------------------------------------------------
    def fail_stale_running(self, *, older_than: datetime) -> int:
        return self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, failure_code = 'stale_running_job', "
            "failure_message = 'Job did not finish before startup recovery.', updated_at = %s, "
            "finished_at = %s WHERE status = %s AND updated_at < %s",
            (JOB_STATUS_FAILED, utc_now(), utc_now(), JOB_STATUS_RUNNING, older_than),
        )

    def fail_orphaned_pending(self, *, older_than: datetime) -> int:
        return self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, failure_code = 'orphaned_pending_job', "
            "failure_message = 'Job was never claimed by a worker.', updated_at = %s, "
            "finished_at = %s WHERE status = %s AND claimed_at IS NULL AND created_at < %s",
            (JOB_STATUS_FAILED, utc_now(), utc_now(), JOB_STATUS_PENDING, older_than),
        )


class DocumentContentRepository:
    """Stores normalized Document text so Re-index does not depend on the original upload."""

    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def upsert(
        self,
        *,
        document_id: str,
        extracted_text: str,
        parser_provider: str,
        truncated: bool,
    ) -> None:
        now = utc_now()
        self.pool.execute(
            "INSERT INTO document_contents (document_id, extracted_text, parser_provider, "
            "char_count, truncated, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE extracted_text = VALUES(extracted_text), "
            "parser_provider = VALUES(parser_provider), char_count = VALUES(char_count), "
            "truncated = VALUES(truncated), updated_at = VALUES(updated_at)",
            (
                document_id,
                extracted_text,
                parser_provider[:40],
                len(extracted_text),
                1 if truncated else 0,
                now,
                now,
            ),
        )

    def get(self, document_id: str) -> str | None:
        row = self.pool.query_one(
            "SELECT extracted_text FROM document_contents WHERE document_id = %s", (document_id,)
        )
        return str(row[0]) if row else None


class ChunkRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def add_many(self, chunks: list[ChunkRecord]) -> None:
        if not chunks:
            return
        now = utc_now()
        with self.pool.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO document_chunks (chunk_id, document_id, index_version, "
                "parent_chunk_id, "
                "chunk_role, ordinal, content, location, label, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.index_version,
                        chunk.parent_chunk_id,
                        chunk.chunk_role,
                        chunk.ordinal,
                        chunk.content,
                        chunk.location,
                        chunk.label,
                        now,
                    )
                    for chunk in chunks
                ],
            )

    def list_by_ids(self, chunk_ids: list[str], *, index_version: str | None) -> list[ChunkRecord]:
        """Fetch chunks by ID; ``index_version=None`` searches across versions.

        Retrieval can span several active versions, so the caller passes ``None`` and
        relies on the returned records plus its own authorization set.
        """

        if not chunk_ids:
            return []
        placeholders = ", ".join(["%s"] * len(chunk_ids))
        version_clause = "AND index_version = %s " if index_version is not None else ""
        parameters: tuple[object, ...] = (
            (index_version, *chunk_ids) if index_version is not None else tuple(chunk_ids)
        )
        rows = self.pool.query(
            "SELECT chunk_id, document_id, index_version, parent_chunk_id, chunk_role, ordinal, "
            f"content, location, label FROM document_chunks WHERE 1 = 1 {version_clause}"
            f"AND chunk_id IN ({placeholders})",
            parameters,
        )
        return [_chunk(row) for row in rows]

    def delete_for_version(self, index_version: str) -> int:
        return self.pool.execute(
            "DELETE FROM document_chunks WHERE index_version = %s", (index_version,)
        )


def _document(row: tuple[Any, ...] | None) -> DocumentRecord | None:
    if row is None:
        return None
    return DocumentRecord(
        document_id=str(row[0]),
        user_id=str(row[1]),
        title=str(row[2]),
        source_name=str(row[3]),
        content_type=str(row[4]),
        content_fingerprint=str(row[5]),
        byte_size=int(row[6]),
        status=str(row[7]),
        active_index_version=row[8],
        created_at=row[9],
        updated_at=row[10],
        deleted_at=row[11],
    )


def _index_version(row: tuple[Any, ...] | None) -> IndexVersionRecord | None:
    if row is None:
        return None
    return IndexVersionRecord(
        index_version=str(row[0]),
        document_id=str(row[1]),
        status=str(row[2]),
        chunk_count=int(row[3]),
        embedding_provider=str(row[4]),
        embedding_model=str(row[5]),
        embedding_dimension=int(row[6]),
        collection_name=str(row[7]),
        parser_provider=str(row[8]),
        failure_code=row[9],
        created_at=row[10],
        updated_at=row[11],
        retired_at=row[12],
    )


def _job(row: tuple[Any, ...] | None) -> IngestionJobRecord | None:
    if row is None:
        return None
    return IngestionJobRecord(
        ingestion_job_id=str(row[0]),
        document_id=str(row[1]),
        index_version=str(row[2]),
        job_type=str(row[3]),
        status=str(row[4]),
        attempt=int(row[5]),
        failure_code=row[6],
        failure_message=row[7],
        claimed_at=row[8],
        created_at=row[9],
        updated_at=row[10],
        finished_at=row[11],
    )


def _chunk(row: tuple[Any, ...]) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=str(row[0]),
        document_id=str(row[1]),
        index_version=str(row[2]),
        parent_chunk_id=row[3],
        chunk_role=str(row[4]),
        ordinal=int(row[5]),
        content=str(row[6]),
        location=row[7],
        label=str(row[8]),
    )
