"""Document、index-version、chunk 与 ingestion-job 的持久化（ADR-019）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.domain.identifiers import cursor_from_utc, cursor_to_utc, new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool

INDEX_STATUS_ACTIVE = "active"
INDEX_STATUS_INDEXING = "indexing"
INDEX_STATUS_RETIRED = "retired"
INDEX_STATUS_FAILED = "failed"

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCESS = "success"
JOB_STATUS_FAILED = "failed"

# Document 生命周期（ADR-040 / CC-API-003）：
#   indexing  已接受并持久化，但还没有 promote 过的版本 —— 不可检索、不可作为作用域
#   available 有合法 active 版本 —— **唯一**可检索、可被选为作用域的取值
#   failed    摄取结束但没有可用版本 —— 不可检索，应重试或删除
#   deleted   终态，promote 永远不会重新激活
DOCUMENT_STATUS_INDEXING = "indexing"
DOCUMENT_STATUS_AVAILABLE = "available"
DOCUMENT_STATUS_FAILED = "failed"
DOCUMENT_STATUS_DELETED = "deleted"

# 分片 manifest 状态机（ADR-037）：pending -> running -> success | failed。
# ``reused`` 是 success 的一个子情形（命中复用键，未调用 provider），不是独立状态，
# 这样「分片闭合 = 全部 success」的完整性判据保持单一。
SHARD_STATUS_PENDING = "pending"
SHARD_STATUS_RUNNING = "running"
SHARD_STATUS_SUCCESS = "success"
SHARD_STATUS_FAILED = "failed"

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
DOCUMENT_FILE_COLUMNS = (
    "document_id, storage_key, sha256, byte_size, content_type, source_name, created_at, updated_at"
)
SHARD_COLUMNS = (
    "shard_id, ingestion_job_id, document_id, index_version, shard_index, page_start, page_end, "
    "byte_size, sha256, parser_provider, parser_options, provider_batch_id, status, reused, "
    "attempt, char_count, failure_code, created_at, updated_at, finished_at"
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
class DocumentFileRecord:
    """原文件引用：受控 storage_key + 内容特性（不含用户路径）。"""

    document_id: str
    storage_key: str
    sha256: str
    byte_size: int
    content_type: str
    source_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IngestionShardRecord:
    """一个分片的 manifest 行（ADR-037）：页范围、字节、复用键与 provider 任务凭据。

    ``extracted_text`` 只在显式请求时读取（``with_text=True``），因为分片文本可能很大；
    ``reused`` 表示这一片没有调用 provider，而是命中了复用键。
    """

    shard_id: str
    ingestion_job_id: str
    document_id: str
    index_version: str
    shard_index: int
    page_start: int | None
    page_end: int | None
    byte_size: int
    sha256: str
    parser_provider: str
    parser_options: str
    provider_batch_id: str | None
    status: str
    reused: bool
    attempt: int
    char_count: int
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    extracted_text: str | None = None
    blocks_json: str | None = None

    @property
    def page_count(self) -> int:
        if self.page_start is None or self.page_end is None:
            return 0
        return self.page_end - self.page_start + 1


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
    # 结构元数据（ADR-038）：无可靠来源时保持 None/默认值，绝不伪造。
    section_path: str | None = None
    block_type: str = "paragraph"
    page_start: int | None = None
    page_end: int | None = None
    content_hash: str | None = None
    token_count: int = 0


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
        # title/source_name/content_type 按 documents 表列宽显式截断，避免超长写入被拒。
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
                # 上传即 available 会让「已上传」与「可检索」混为一谈（G19）：
                # 新文档一律从 indexing 开始，promote 才是唯一进入 available 的路径。
                DOCUMENT_STATUS_INDEXING,
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
            DOCUMENT_STATUS_INDEXING,
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
            # MySQL DATETIME(6) is stored as naive UTC.  Bind the decoded UTC
            # datetime instead of using FROM_UNIXTIME(), whose result depends
            # on the connection/session timezone.
            clause = "AND created_at < %s"
            parameters.append(cursor_to_utc(cursor))
        parameters.append(limit + 1)
        rows = self.pool.query(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents "
            f"WHERE user_id = %s AND status <> 'deleted' {clause} "
            "ORDER BY created_at DESC, document_id DESC LIMIT %s",
            parameters,
        )
        items = [record for row in rows[:limit] if (record := _document(row)) is not None]
        next_cursor = cursor_from_utc(items[-1].created_at) if len(rows) > limit and items else None
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

    def activate(self, document_id: str, index_version: str) -> bool:
        """把文档原子地置为 ``available`` 并绑定 active 版本（ADR-040 决策 2）。

        状态与 active 版本写在**同一条**条件 UPDATE 里，因此二者不会出现不一致的中间态；
        ``deleted`` 的文档永远不会被重新激活。
        """

        affected = self.pool.execute(
            "UPDATE documents SET status = %s, active_index_version = %s, updated_at = %s "
            "WHERE document_id = %s AND status <> %s",
            (
                DOCUMENT_STATUS_AVAILABLE,
                index_version,
                utc_now(),
                document_id,
                DOCUMENT_STATUS_DELETED,
            ),
        )
        return affected > 0

    def mark_failed(self, document_id: str) -> bool:
        """摄取失败后把**没有可用版本**的文档置为 ``failed``（ADR-040 决策 3）。

        带 ``active_index_version IS NULL`` 守卫：失败的**重建索引**绝不能降级一个仍在
        用旧版本服务的文档。
        """

        affected = self.pool.execute(
            "UPDATE documents SET status = %s, updated_at = %s "
            "WHERE document_id = %s AND status <> %s AND active_index_version IS NULL",
            (
                DOCUMENT_STATUS_FAILED,
                utc_now(),
                document_id,
                DOCUMENT_STATUS_DELETED,
            ),
        )
        return affected > 0

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

    def claim(self, ingestion_job_id: str, *, owner_token: str) -> bool:
        """把 ``pending`` Job 原子地转换为 ``running`` 并记录执行所有权。

        领取是唯一设置 owner 的入口（``pending`` 唯一可领取），因此一个 Job 最多被
        一个 worker 执行；``owner_token`` 供其后所有提交点做所有权校验（ADR-035）。
        """

        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, owner_token = %s, claimed_at = %s, "
            "heartbeat_at = %s, updated_at = %s "
            "WHERE ingestion_job_id = %s AND status = %s",
            (
                JOB_STATUS_RUNNING,
                owner_token[:64],
                now,
                now,
                now,
                ingestion_job_id,
                JOB_STATUS_PENDING,
            ),
        )
        return affected > 0

    def heartbeat(self, ingestion_job_id: str, *, owner_token: str) -> bool:
        """独立心跳续租：仅当 Job 仍是 ``running`` 且仍由该 token 持有时推进租约。"""

        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_jobs SET heartbeat_at = %s, updated_at = %s "
            "WHERE ingestion_job_id = %s AND status = %s AND owner_token = %s",
            (now, now, ingestion_job_id, JOB_STATUS_RUNNING, owner_token),
        )
        return affected > 0

    def still_owned(self, ingestion_job_id: str, *, owner_token: str) -> bool:
        """该 Job 是否仍处于 ``running`` 且仍归属于该 token（promote/清理提交点校验）。"""

        row = self.pool.query_one(
            "SELECT 1 FROM ingestion_jobs "
            "WHERE ingestion_job_id = %s AND status = %s AND owner_token = %s",
            (ingestion_job_id, JOB_STATUS_RUNNING, owner_token),
        )
        return row is not None

    def finish(
        self,
        ingestion_job_id: str,
        *,
        status: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """写入 Job 终态；执行者路径必须给出 ``owner_token``（提交点所有权校验）。

        给出 token 时是条件更新（``status = running AND owner_token = ?``）：已被陈旧
        恢复撤销所有权的旧 worker 无法再写终态。``owner_token=None`` 仅供管理/测试
        的显式终结使用（例如强制制造一次失败）。终态行一律清空 owner。
        """

        now = utc_now()
        parameters: list[Any] = [
            status,
            failure_code[:64] if failure_code else None,
            failure_message[:500] if failure_message else None,
            now,
            now,
            ingestion_job_id,
        ]
        clause = ""
        if owner_token is not None:
            clause = " AND status = %s AND owner_token = %s"
            parameters.extend([JOB_STATUS_RUNNING, owner_token])
        affected = self.pool.execute(
            "UPDATE ingestion_jobs SET status = %s, failure_code = %s, failure_message = %s, "
            "owner_token = NULL, updated_at = %s, finished_at = %s "
            f"WHERE ingestion_job_id = %s{clause}",
            parameters,
        )
        return affected > 0

    def max_attempt(self, document_id: str) -> int:
        row = self.pool.query_one(
            "SELECT MAX(attempt) FROM ingestion_jobs WHERE document_id = %s", (document_id,)
        )
        return int(row[0]) if row and row[0] is not None else 0

    # 启动恢复 ------------------------------------------------------
    def fail_stale_running(self, *, older_than: datetime) -> int:
        """把租约已陈旧的 ``running`` Job 终结为 ``failed`` 并撤销其执行所有权。

        陈旧判定基于 ``COALESCE(heartbeat_at, updated_at)``：由独立心跳线程续租的
        ``heartbeat_at`` 是租约权威时间戳（单步长阻塞也不影响），未迁移的旧行退回
        ``updated_at``。撤销所有权（``owner_token = NULL``）与对应的索引版本一同失败，
        使旧 worker 的 finish/promote 变成无副作用的条件更新失败（ADR-035）。
        """

        now = utc_now()
        with self.pool.transaction() as cursor:
            cursor.execute(
                "SELECT ingestion_job_id, index_version FROM ingestion_jobs "
                "WHERE status = %s AND COALESCE(heartbeat_at, updated_at) < %s",
                (JOB_STATUS_RUNNING, older_than),
            )
            rows = list(cursor.fetchall())
            if not rows:
                return 0
            job_ids = [str(row[0]) for row in rows]
            versions = [str(row[1]) for row in rows]
            job_placeholders = ", ".join(["%s"] * len(job_ids))
            cursor.execute(
                "UPDATE ingestion_jobs SET status = %s, owner_token = NULL, "
                "failure_code = 'stale_running_job', "
                "failure_message = 'Job did not finish before startup recovery.', "
                f"updated_at = %s, finished_at = %s WHERE ingestion_job_id IN ({job_placeholders})",
                (JOB_STATUS_FAILED, now, now, *job_ids),
            )
            version_placeholders = ", ".join(["%s"] * len(versions))
            # 从未 promote 过的版本随 Job 一起失败；已 active 的版本不受影响。
            cursor.execute(
                "UPDATE document_index_versions SET status = %s, "
                "failure_code = 'stale_running_job', "
                f"updated_at = %s WHERE index_version IN ({version_placeholders}) "
                "AND status <> 'active'",
                (INDEX_STATUS_FAILED, now, *versions),
            )
            # 文档也要有诚实的终态（ADR-040 决策 3）：只有**没有可用版本**的文档才降级，
            # 因此陈旧恢复不会影响仍在服务旧版本的文档。
            document_placeholders = ", ".join(["%s"] * len(versions))
            cursor.execute(
                "UPDATE documents SET status = %s, updated_at = %s "
                f"WHERE status <> %s AND active_index_version IS NULL "
                f"AND document_id IN (SELECT document_id FROM ingestion_jobs "
                f"WHERE index_version IN ({document_placeholders}))",
                (DOCUMENT_STATUS_FAILED, now, DOCUMENT_STATUS_DELETED, *versions),
            )
        return len(job_ids)

    def fail_orphaned_pending(self, *, older_than: datetime) -> int:
        """把从未被领取的 ``pending`` Job 收口为 ``failed``，并同步文档终态。

        文档侧同样带 ``active_index_version IS NULL`` 守卫（ADR-040 决策 3）。
        """

        now = utc_now()
        with self.pool.transaction() as cursor:
            cursor.execute(
                "SELECT ingestion_job_id, document_id FROM ingestion_jobs "
                "WHERE status = %s AND claimed_at IS NULL AND created_at < %s",
                (JOB_STATUS_PENDING, older_than),
            )
            rows = list(cursor.fetchall())
            if not rows:
                return 0
            job_ids = [str(row[0]) for row in rows]
            document_ids = [str(row[1]) for row in rows]
            placeholders = ", ".join(["%s"] * len(job_ids))
            cursor.execute(
                "UPDATE ingestion_jobs SET status = %s, failure_code = 'orphaned_pending_job', "
                "failure_message = 'Job was never claimed by a worker.', updated_at = %s, "
                f"finished_at = %s WHERE ingestion_job_id IN ({placeholders})",
                (JOB_STATUS_FAILED, now, now, *job_ids),
            )
            document_placeholders = ", ".join(["%s"] * len(document_ids))
            cursor.execute(
                "UPDATE documents SET status = %s, updated_at = %s "
                f"WHERE document_id IN ({document_placeholders}) AND status <> %s "
                "AND active_index_version IS NULL",
                (DOCUMENT_STATUS_FAILED, now, *document_ids, DOCUMENT_STATUS_DELETED),
            )
        return len(job_ids)

    def list_pending(self, *, older_than: datetime, limit: int) -> list[str]:
        """等待超过阈值仍未领取的 pending Job（维护任务的补调度候选，有界）。

        只挑「从未被领取」的行（``claimed_at IS NULL``）：已被 worker 领取的任务由其
        自身完成或由陈旧恢复处理，补调度不会重复执行。
        """

        rows = self.pool.query(
            "SELECT ingestion_job_id FROM ingestion_jobs "
            "WHERE status = %s AND claimed_at IS NULL AND created_at < %s "
            "ORDER BY created_at ASC LIMIT %s",
            (JOB_STATUS_PENDING, older_than, limit),
        )
        return [str(row[0]) for row in rows]


class DocumentFileRepository:
    """受控存储中的原文件引用（CR-RUNTIME-20261009-01）。

    只保存受控 ``storage_key``（内容寻址），绝不保存用户提供的路径；解析、重试与
    后续分片都从这里取原始字节，因此重启后 Job 仍然可执行。
    """

    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def upsert(
        self,
        *,
        document_id: str,
        storage_key: str,
        sha256: str,
        byte_size: int,
        content_type: str,
        source_name: str,
    ) -> DocumentFileRecord:
        now = utc_now()
        self.pool.execute(
            "INSERT INTO document_files (document_id, storage_key, sha256, byte_size, "
            "content_type, source_name, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE storage_key = VALUES(storage_key), sha256 = VALUES(sha256), "
            "byte_size = VALUES(byte_size), content_type = VALUES(content_type), "
            "source_name = VALUES(source_name), updated_at = VALUES(updated_at)",
            (
                document_id,
                storage_key[:512],
                sha256,
                byte_size,
                content_type[:100],
                source_name[:300],
                now,
                now,
            ),
        )
        return DocumentFileRecord(
            document_id=document_id,
            storage_key=storage_key,
            sha256=sha256,
            byte_size=byte_size,
            content_type=content_type[:100],
            source_name=source_name[:300],
            created_at=now,
            updated_at=now,
        )

    def get(self, document_id: str) -> DocumentFileRecord | None:
        row = self.pool.query_one(
            f"SELECT {DOCUMENT_FILE_COLUMNS} FROM document_files WHERE document_id = %s",
            (document_id,),
        )
        return _document_file(row)

    def count_by_storage_key(self, storage_key: str) -> int:
        """统计仍引用同一受控对象的行数（内容寻址下删除前必须先确认无其它引用）。"""

        row = self.pool.query_one(
            "SELECT COUNT(*) FROM document_files WHERE storage_key = %s", (storage_key,)
        )
        return int(row[0]) if row else 0

    def delete(self, document_id: str) -> bool:
        affected = self.pool.execute(
            "DELETE FROM document_files WHERE document_id = %s", (document_id,)
        )
        return affected > 0


class IngestionShardRepository:
    """分片 manifest 的持久化（ADR-037）。

    它同时承担三件事，缺一不可：

    1. **闭合**：``UNIQUE(index_version, shard_index)`` 保证一个 index version 的每个
       分片序号唯一，完整性校验据此证明「无缺片、无重叠」。
    2. **复用**：``find_reusable`` 按 ``(sha256, parser_provider, parser_options)`` 命中
       已成功的分片，直接复制文本、**不调用 provider**（成功分片不重复付费解析）。
    3. **恢复**：``find_resumable`` 找回「已 submit、但轮询超时」的分片 batch id，
       重试时续轮询同一个 provider 任务，绝不重复提交。
    """

    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        ingestion_job_id: str,
        document_id: str,
        index_version: str,
        shard_index: int,
        byte_size: int,
        sha256: str,
        parser_provider: str,
        parser_options: str,
        page_start: int | None = None,
        page_end: int | None = None,
        provider_batch_id: str | None = None,
        blocks_json: str | None = None,
    ) -> IngestionShardRecord:
        shard_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO ingestion_shards (shard_id, ingestion_job_id, document_id, index_version, "
            "shard_index, page_start, page_end, byte_size, sha256, parser_provider, "
            "parser_options, provider_batch_id, status, reused, attempt, char_count, blocks_json, "
            "created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, 0, %s, %s, %s)",
            (
                shard_id,
                ingestion_job_id,
                document_id,
                index_version,
                shard_index,
                page_start,
                page_end,
                byte_size,
                sha256,
                parser_provider[:40],
                parser_options[:120],
                provider_batch_id[:120] if provider_batch_id else None,
                SHARD_STATUS_PENDING,
                blocks_json,
                now,
                now,
            ),
        )
        return IngestionShardRecord(
            shard_id=shard_id,
            ingestion_job_id=ingestion_job_id,
            document_id=document_id,
            index_version=index_version,
            shard_index=shard_index,
            page_start=page_start,
            page_end=page_end,
            byte_size=byte_size,
            sha256=sha256,
            parser_provider=parser_provider[:40],
            parser_options=parser_options[:120],
            provider_batch_id=provider_batch_id,
            status=SHARD_STATUS_PENDING,
            reused=False,
            attempt=0,
            char_count=0,
            failure_code=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
            blocks_json=blocks_json,
        )

    def get(self, shard_id: str, *, with_text: bool = False) -> IngestionShardRecord | None:
        columns = SHARD_COLUMNS + (", extracted_text, blocks_json" if with_text else "")
        row = self.pool.query_one(
            f"SELECT {columns} FROM ingestion_shards WHERE shard_id = %s", (shard_id,)
        )
        return _shard(row, with_text=with_text)

    def get_by_index(
        self, *, index_version: str, shard_index: int, with_text: bool = False
    ) -> IngestionShardRecord | None:
        """按 ``(index_version, shard_index)`` 取分片：同一版本内的重试原地推进 attempt。"""

        columns = SHARD_COLUMNS + (", extracted_text, blocks_json" if with_text else "")
        row = self.pool.query_one(
            f"SELECT {columns} FROM ingestion_shards WHERE index_version = %s AND shard_index = %s",
            (index_version, shard_index),
        )
        return _shard(row, with_text=with_text)

    def list_for_job(
        self, ingestion_job_id: str, *, with_text: bool = False
    ) -> list[IngestionShardRecord]:
        columns = SHARD_COLUMNS + (", extracted_text, blocks_json" if with_text else "")
        rows = self.pool.query(
            f"SELECT {columns} FROM ingestion_shards WHERE ingestion_job_id = %s "
            "ORDER BY shard_index ASC",
            (ingestion_job_id,),
        )
        return [record for row in rows if (record := _shard(row, with_text=with_text)) is not None]

    def list_for_version(
        self, index_version: str, *, with_text: bool = False
    ) -> list[IngestionShardRecord]:
        columns = SHARD_COLUMNS + (", extracted_text, blocks_json" if with_text else "")
        rows = self.pool.query(
            f"SELECT {columns} FROM ingestion_shards WHERE index_version = %s "
            "ORDER BY shard_index ASC",
            (index_version,),
        )
        return [record for row in rows if (record := _shard(row, with_text=with_text)) is not None]

    def claim(self, shard_id: str) -> bool:
        """``pending`` -> ``running``（并累加 attempt）；已是其它状态则不重复领取。"""

        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_shards SET status = %s, attempt = attempt + 1, updated_at = %s "
            "WHERE shard_id = %s AND status = %s",
            (SHARD_STATUS_RUNNING, now, shard_id, SHARD_STATUS_PENDING),
        )
        return affected > 0

    def set_provider_batch_id(self, shard_id: str, *, provider_batch_id: str) -> bool:
        """submit 之后**立刻**写入 batch id：轮询超时时它是恢复的唯一凭据。"""

        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_shards SET provider_batch_id = %s, updated_at = %s "
            "WHERE shard_id = %s",
            (provider_batch_id[:120], now, shard_id),
        )
        return affected > 0

    def mark_success(
        self,
        shard_id: str,
        *,
        extracted_text: str,
        reused: bool = False,
        blocks_json: str | None = None,
    ) -> bool:
        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_shards SET status = %s, extracted_text = %s, char_count = %s, "
            "reused = %s, blocks_json = %s, failure_code = NULL, updated_at = %s, "
            "finished_at = %s WHERE shard_id = %s AND status <> %s",
            (
                SHARD_STATUS_SUCCESS,
                extracted_text,
                len(extracted_text),
                1 if reused else 0,
                blocks_json,
                now,
                now,
                shard_id,
                SHARD_STATUS_SUCCESS,
            ),
        )
        return affected > 0

    def mark_failed(self, shard_id: str, *, failure_code: str) -> bool:
        now = utc_now()
        affected = self.pool.execute(
            "UPDATE ingestion_shards SET status = %s, failure_code = %s, updated_at = %s, "
            "finished_at = %s WHERE shard_id = %s AND status <> %s",
            (SHARD_STATUS_FAILED, failure_code[:64], now, now, shard_id, SHARD_STATUS_SUCCESS),
        )
        return affected > 0

    def find_reusable(
        self, *, sha256: str, parser_provider: str, parser_options: str
    ) -> IngestionShardRecord | None:
        """复用键命中：同字节 + 同 provider + 同 options 的**成功**分片。

        跨 index version、跨文档生效——只要字节与解析档位一致，解析结果就一致，
        因此可以跳过 provider（ADR-037「成功分片不重复付费解析」）。
        """

        row = self.pool.query_one(
            f"SELECT {SHARD_COLUMNS}, extracted_text, blocks_json FROM ingestion_shards "
            "WHERE sha256 = %s AND parser_provider = %s AND parser_options = %s "
            "AND status = %s AND extracted_text IS NOT NULL "
            "ORDER BY finished_at DESC, created_at DESC LIMIT 1",
            (sha256, parser_provider[:40], parser_options[:120], SHARD_STATUS_SUCCESS),
        )
        return _shard(row, with_text=True)

    def find_resumable(
        self, *, sha256: str, parser_provider: str, parser_options: str
    ) -> IngestionShardRecord | None:
        """找回已提交但未完成的分片：同键 + ``running`` + 有 batch id。

        这就是「超时 ≠ 未创建」的落地方式：重试先续轮询这个 batch id，而不是重新提交。
        """

        row = self.pool.query_one(
            f"SELECT {SHARD_COLUMNS} FROM ingestion_shards "
            "WHERE sha256 = %s AND parser_provider = %s AND parser_options = %s "
            "AND status = %s AND provider_batch_id IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (sha256, parser_provider[:40], parser_options[:120], SHARD_STATUS_RUNNING),
        )
        return _shard(row, with_text=False)

    def delete_for_index_version(self, index_version: str) -> int:
        return self.pool.execute(
            "DELETE FROM ingestion_shards WHERE index_version = %s", (index_version,)
        )


def _document_file(row: tuple[Any, ...] | None) -> DocumentFileRecord | None:
    if row is None:
        return None
    return DocumentFileRecord(
        document_id=str(row[0]),
        storage_key=str(row[1]),
        sha256=str(row[2]),
        byte_size=int(row[3]),
        content_type=str(row[4]),
        source_name=str(row[5]),
        created_at=row[6],
        updated_at=row[7],
    )


def _shard(row: tuple[Any, ...] | None, *, with_text: bool) -> IngestionShardRecord | None:
    if row is None:
        return None
    return IngestionShardRecord(
        shard_id=str(row[0]),
        ingestion_job_id=str(row[1]),
        document_id=str(row[2]),
        index_version=str(row[3]),
        shard_index=int(row[4]),
        page_start=int(row[5]) if row[5] is not None else None,
        page_end=int(row[6]) if row[6] is not None else None,
        byte_size=int(row[7]),
        sha256=str(row[8]),
        parser_provider=str(row[9]),
        parser_options=str(row[10]),
        provider_batch_id=row[11],
        status=str(row[12]),
        reused=bool(row[13]),
        attempt=int(row[14]),
        char_count=int(row[15]),
        failure_code=row[16],
        created_at=row[17],
        updated_at=row[18],
        finished_at=row[19],
        extracted_text=str(row[20]) if with_text and row[20] is not None else None,
        blocks_json=str(row[21]) if with_text and len(row) > 21 and row[21] is not None else None,
    )


class DocumentContentRepository:
    """保存规范化后的 Document 文本，使 Re-index 不依赖原始上传件。"""

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


# document_chunks.location 的列宽（与建表语句中的 VARCHAR(120) 对齐）。
# 写入前显式截断，不依赖「当前定位串恰好很短」这一隐含假设。
MAX_LOCATION_CHARS = 120
# document_chunks.section_path 的列宽（ADR-038）。
MAX_SECTION_PATH_CHARS = 300


def bounded_location(location: str | None) -> str | None:
    """把分块定位串显式约束到 document_chunks.location 的列宽。"""

    if location is None:
        return None
    return location[:MAX_LOCATION_CHARS]


def bounded_section_path(section_path: str | None) -> str | None:
    """把章节路径显式约束到 document_chunks.section_path 的列宽。"""

    if section_path is None:
        return None
    return section_path[:MAX_SECTION_PATH_CHARS]


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
                "chunk_role, ordinal, content, location, label, section_path, block_type, "
                "page_start, page_end, content_hash, token_count, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.index_version,
                        chunk.parent_chunk_id,
                        chunk.chunk_role,
                        chunk.ordinal,
                        chunk.content,
                        bounded_location(chunk.location),
                        chunk.label,
                        bounded_section_path(chunk.section_path),
                        chunk.block_type[:20],
                        chunk.page_start,
                        chunk.page_end,
                        chunk.content_hash,
                        chunk.token_count,
                        now,
                    )
                    for chunk in chunks
                ],
            )

    def list_by_ids(self, chunk_ids: list[str], *, index_version: str | None) -> list[ChunkRecord]:
        """按 ID 获取 chunk；``index_version=None`` 时跨版本搜索。

        Retrieval 可能跨越多个活跃版本，因此调用方传入 ``None``，并依赖返回记录与自身的
        授权集合。
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
            "content, location, label, section_path, block_type, page_start, page_end, "
            f"content_hash, token_count FROM document_chunks WHERE 1 = 1 {version_clause}"
            f"AND chunk_id IN ({placeholders})",
            parameters,
        )
        return [_chunk(row) for row in rows]

    def delete_for_version(self, index_version: str) -> int:
        return self.pool.execute(
            "DELETE FROM document_chunks WHERE index_version = %s", (index_version,)
        )

    def count_for_version(self, index_version: str, *, chunk_role: str | None = None) -> int:
        """该 index version 的分块行数（可按角色过滤）；完整性校验用（ADR-036 决策 4）。"""

        parameters: list[Any] = [index_version]
        clause = ""
        if chunk_role is not None:
            clause = " AND chunk_role = %s"
            parameters.append(chunk_role)
        row = self.pool.query_one(
            f"SELECT COUNT(*) FROM document_chunks WHERE index_version = %s{clause}", parameters
        )
        return int(row[0]) if row else 0


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
        section_path=row[9] if len(row) > 9 else None,
        block_type=str(row[10]) if len(row) > 10 and row[10] else "paragraph",
        page_start=int(row[11]) if len(row) > 11 and row[11] is not None else None,
        page_end=int(row[12]) if len(row) > 12 and row[12] is not None else None,
        content_hash=row[13] if len(row) > 13 else None,
        token_count=int(row[14]) if len(row) > 14 and row[14] is not None else 0,
    )
