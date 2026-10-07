"""结构化数据持久化：不可变的 Query Result Snapshot 与 export artifact。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.database import MySQLConnectionPool

QUERY_RESULT_COLUMNS = (
    "query_result_id, user_id, conversation_id, run_id, data_source_id, columns_json, rows_json, "
    "row_count, truncated, tables_json, duration_ms, created_at, expires_at"
)
ARTIFACT_COLUMNS = (
    "artifact_id, approval_request_id, query_result_id, user_id, format, row_count, content, "
    "created_at"
)


@dataclass(frozen=True)
class QueryResultRecord:
    query_result_id: str
    user_id: str
    conversation_id: str | None
    run_id: str | None
    data_source_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    tables: list[str]
    duration_ms: int
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ExportArtifactRecord:
    artifact_id: str
    approval_request_id: str
    query_result_id: str
    user_id: str
    format: str
    row_count: int
    content: str
    created_at: datetime


class QueryResultRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def create(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        run_id: str | None,
        data_source_id: str,
        columns: list[str],
        rows: list[list[Any]],
        truncated: bool,
        query_fingerprint: str,
        tables: list[str],
        duration_ms: int,
        retention_seconds: int,
    ) -> QueryResultRecord:
        query_result_id = new_id()
        now = utc_now()
        expires_at = datetime.fromtimestamp(now.timestamp() + retention_seconds)
        self.pool.execute(
            "INSERT INTO query_results (query_result_id, user_id, conversation_id, run_id, "
            "data_source_id, columns_json, rows_json, row_count, truncated, query_fingerprint, "
            "tables_json, duration_ms, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                query_result_id,
                user_id,
                conversation_id,
                run_id,
                data_source_id,
                json.dumps(columns, ensure_ascii=False),
                json.dumps(rows, ensure_ascii=False, default=str),
                len(rows),
                1 if truncated else 0,
                query_fingerprint,
                json.dumps(tables, ensure_ascii=False),
                duration_ms,
                now,
                expires_at,
            ),
        )
        return QueryResultRecord(
            query_result_id=query_result_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            data_source_id=data_source_id,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            tables=tables,
            duration_ms=duration_ms,
            created_at=now,
            expires_at=expires_at,
        )

    def get(self, query_result_id: str) -> QueryResultRecord | None:
        row = self.pool.query_one(
            f"SELECT {QUERY_RESULT_COLUMNS} FROM query_results WHERE query_result_id = %s",
            (query_result_id,),
        )
        return _query_result(row)

    def delete_expired(self, *, limit: int = 200) -> int:
        return self.pool.execute(
            "DELETE FROM query_results WHERE expires_at <= %s LIMIT %s", (utc_now(), limit)
        )

    def clear_run_reference(self, run_id: str) -> None:
        """解除 Run 引用但不删除不可变的 snapshot。"""

        self.pool.execute("UPDATE query_results SET run_id = NULL WHERE run_id = %s", (run_id,))


class ExportArtifactRepository:
    def __init__(self, pool: MySQLConnectionPool) -> None:
        self.pool = pool

    def insert_ignore(
        self,
        *,
        artifact_id: str,
        approval_request_id: str,
        query_result_id: str,
        user_id: str,
        format: str,
        row_count: int,
        content: str,
    ) -> bool:
        """每个 approval request 至多创建一次 Artifact。

        本调用创建了该行时返回 ``True``；重复调用保留已有行并返回 ``False``，以免重复副作用。
        """

        affected = self.pool.execute(
            "INSERT IGNORE INTO export_artifacts (artifact_id, approval_request_id, "
            "query_result_id, user_id, format, row_count, content, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                artifact_id,
                approval_request_id,
                query_result_id,
                user_id,
                format[:10],
                row_count,
                content,
                utc_now(),
            ),
        )
        return affected > 0

    def create(
        self,
        *,
        approval_request_id: str,
        query_result_id: str,
        user_id: str,
        format: str,
        row_count: int,
        content: str,
    ) -> ExportArtifactRecord:
        artifact_id = new_id()
        now = utc_now()
        self.pool.execute(
            "INSERT INTO export_artifacts (artifact_id, approval_request_id, query_result_id, "
            "user_id, "
            "format, row_count, content, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                artifact_id,
                approval_request_id,
                query_result_id,
                user_id,
                format[:10],
                row_count,
                content,
                now,
            ),
        )
        return ExportArtifactRecord(
            artifact_id,
            approval_request_id,
            query_result_id,
            user_id,
            format[:10],
            row_count,
            content,
            now,
        )

    def get_by_approval(self, approval_request_id: str) -> ExportArtifactRecord | None:
        row = self.pool.query_one(
            f"SELECT {ARTIFACT_COLUMNS} FROM export_artifacts WHERE approval_request_id = %s",
            (approval_request_id,),
        )
        if row is None:
            return None
        return ExportArtifactRecord(
            artifact_id=str(row[0]),
            approval_request_id=str(row[1]),
            query_result_id=str(row[2]),
            user_id=str(row[3]),
            format=str(row[4]),
            row_count=int(row[5]),
            content=str(row[6]),
            created_at=row[7],
        )


def _query_result(row: tuple[Any, ...] | None) -> QueryResultRecord | None:
    if row is None:
        return None
    return QueryResultRecord(
        query_result_id=str(row[0]),
        user_id=str(row[1]),
        conversation_id=row[2],
        run_id=row[3],
        data_source_id=str(row[4]),
        columns=json.loads(row[5]) if row[5] else [],
        rows=json.loads(row[6]) if row[6] else [],
        row_count=int(row[7]),
        truncated=bool(row[8]),
        tables=json.loads(row[9]) if row[9] else [],
        duration_ms=int(row[10]),
        created_at=row[11],
        expires_at=row[12],
    )
