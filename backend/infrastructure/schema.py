"""应用数据库的 schema 定义与安全引导。

此处的 DDL 属于实现细节，不重新定义任何 Core Contract。每条语句都是幂等的，
因此启动初始化可以反复执行。
"""

from __future__ import annotations

import logging

import pymysql

from backend.infrastructure.config import Settings
from backend.infrastructure.database import MySQLConnectionPool, PoolSettings

logger = logging.getLogger(__name__)

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
      user_id VARCHAR(36) NOT NULL PRIMARY KEY,
      username VARCHAR(64) NOT NULL,
      display_name VARCHAR(128) NOT NULL,
      password_hash VARCHAR(255) NOT NULL,
      password_salt VARCHAR(64) NOT NULL,
      is_active TINYINT(1) NOT NULL DEFAULT 1,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      UNIQUE KEY uq_users_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
      session_id VARCHAR(36) NOT NULL PRIMARY KEY,
      user_id VARCHAR(36) NOT NULL,
      token_hash CHAR(64) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      expires_at DATETIME(6) NOT NULL,
      revoked_at DATETIME(6) NULL,
      UNIQUE KEY uq_auth_sessions_token (token_hash),
      KEY ix_auth_sessions_user (user_id, expires_at),
      CONSTRAINT fk_auth_sessions_user FOREIGN KEY (user_id) REFERENCES users (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
      conversation_id VARCHAR(36) NOT NULL PRIMARY KEY,
      user_id VARCHAR(36) NOT NULL,
      title VARCHAR(200) NOT NULL,
      status VARCHAR(20) NOT NULL DEFAULT 'active',
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      deleted_at DATETIME(6) NULL,
      KEY ix_conversations_user (user_id, status, created_at),
      CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_messages (
      message_id VARCHAR(36) NOT NULL PRIMARY KEY,
      conversation_id VARCHAR(36) NOT NULL,
      run_id VARCHAR(36) NULL,
      role VARCHAR(20) NOT NULL,
      content MEDIUMTEXT NOT NULL,
      status VARCHAR(20) NOT NULL,
      citations_json JSON NULL,
      metadata_json JSON NULL,
      created_at DATETIME(6) NOT NULL,
      KEY ix_messages_conversation (conversation_id, created_at),
      CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations (conversation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
      thread_id VARCHAR(36) NOT NULL PRIMARY KEY,
      conversation_id VARCHAR(36) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      KEY ix_threads_conversation (conversation_id, created_at),
      CONSTRAINT fk_threads_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations (conversation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id VARCHAR(36) NOT NULL PRIMARY KEY,
      conversation_id VARCHAR(36) NOT NULL,
      thread_id VARCHAR(36) NOT NULL,
      user_id VARCHAR(36) NOT NULL,
      user_input TEXT NOT NULL,
      status VARCHAR(32) NOT NULL,
      -- 执行所有权（CR-RUNTIME-20261009-01 / ADR-035）：只有持有该 token 的执行者
      -- 才能写事件、等待态与终态；heartbeat_at 是独立心跳线程续租的权威时间戳。
      owner_token VARCHAR(64) NULL,
      heartbeat_at DATETIME(6) NULL,
      routing_outcome VARCHAR(32) NULL,
      intent VARCHAR(32) NULL,
      final_response MEDIUMTEXT NULL,
      error_code VARCHAR(64) NULL,
      error_message VARCHAR(500) NULL,
      approval_request_id VARCHAR(36) NULL,
      original_run_id VARCHAR(36) NULL,
      predecessor_run_id VARCHAR(36) NULL,
      query_result_id VARCHAR(36) NULL,
      client_metadata_json JSON NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      completed_at DATETIME(6) NULL,
      KEY ix_runs_thread (thread_id, created_at),
      KEY ix_runs_conversation (conversation_id, created_at),
      KEY ix_runs_status (status, updated_at),
      CONSTRAINT fk_runs_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversations (conversation_id),
      CONSTRAINT fk_runs_thread FOREIGN KEY (thread_id) REFERENCES threads (thread_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      run_id VARCHAR(36) NOT NULL,
      sequence INT NOT NULL,
      event_id VARCHAR(36) NOT NULL,
      event_type VARCHAR(32) NOT NULL,
      thread_id VARCHAR(36) NOT NULL,
      payload_json JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      UNIQUE KEY uq_run_events_sequence (run_id, sequence),
      UNIQUE KEY uq_run_events_event (event_id),
      CONSTRAINT fk_run_events_run FOREIGN KEY (run_id) REFERENCES runs (run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
      approval_request_id VARCHAR(36) NOT NULL PRIMARY KEY,
      run_id VARCHAR(36) NOT NULL,
      thread_id VARCHAR(36) NOT NULL,
      conversation_id VARCHAR(36) NOT NULL,
      user_id VARCHAR(36) NOT NULL,
      status VARCHAR(20) NOT NULL,
      summary_json JSON NOT NULL,
      binding_json JSON NOT NULL,
      created_at DATETIME(6) NOT NULL,
      expires_at DATETIME(6) NOT NULL,
      decided_at DATETIME(6) NULL,
      decided_by VARCHAR(36) NULL,
      successor_run_id VARCHAR(36) NULL,
      artifact_id VARCHAR(36) NULL,
      side_effect_completed_at DATETIME(6) NULL,
      KEY ix_approvals_run (run_id),
      KEY ix_approvals_status (status, expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
      document_id VARCHAR(36) NOT NULL PRIMARY KEY,
      user_id VARCHAR(36) NOT NULL,
      title VARCHAR(300) NOT NULL,
      source_name VARCHAR(300) NOT NULL,
      content_type VARCHAR(100) NOT NULL,
      content_fingerprint CHAR(64) NOT NULL,
      byte_size INT NOT NULL,
      status VARCHAR(20) NOT NULL,
      active_index_version VARCHAR(36) NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      deleted_at DATETIME(6) NULL,
      KEY ix_documents_user (user_id, status, created_at),
      KEY ix_documents_fingerprint (user_id, content_fingerprint)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS document_files (
      document_id VARCHAR(36) NOT NULL PRIMARY KEY,
      storage_key VARCHAR(512) NOT NULL,
      sha256 CHAR(64) NOT NULL,
      byte_size BIGINT NOT NULL,
      content_type VARCHAR(100) NOT NULL,
      source_name VARCHAR(300) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      KEY ix_document_files_sha (sha256),
      CONSTRAINT fk_document_files_document
        FOREIGN KEY (document_id) REFERENCES documents (document_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS document_index_versions (
      index_version VARCHAR(36) NOT NULL PRIMARY KEY,
      document_id VARCHAR(36) NOT NULL,
      status VARCHAR(20) NOT NULL,
      chunk_count INT NOT NULL DEFAULT 0,
      embedding_provider VARCHAR(40) NOT NULL,
      embedding_model VARCHAR(120) NOT NULL,
      embedding_dimension INT NOT NULL,
      collection_name VARCHAR(120) NOT NULL,
      parser_provider VARCHAR(40) NOT NULL,
      failure_code VARCHAR(64) NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      retired_at DATETIME(6) NULL,
      KEY ix_versions_document (document_id, status),
      CONSTRAINT fk_versions_document FOREIGN KEY (document_id) REFERENCES documents (document_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_jobs (
      ingestion_job_id VARCHAR(36) NOT NULL PRIMARY KEY,
      document_id VARCHAR(36) NOT NULL,
      index_version VARCHAR(36) NOT NULL,
      job_type VARCHAR(20) NOT NULL,
      status VARCHAR(20) NOT NULL,
      attempt INT NOT NULL DEFAULT 1,
      -- 执行所有权（CR-RUNTIME-20261009-01 / ADR-035）：与 runs 同一套 claim/lease/心跳语义。
      owner_token VARCHAR(64) NULL,
      heartbeat_at DATETIME(6) NULL,
      idempotency_key VARCHAR(80) NULL,
      failure_code VARCHAR(64) NULL,
      failure_message VARCHAR(500) NULL,
      claimed_at DATETIME(6) NULL,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      finished_at DATETIME(6) NULL,
      KEY ix_jobs_status (status, updated_at),
      KEY ix_jobs_document (document_id, created_at),
      UNIQUE KEY uq_jobs_idempotency (document_id, idempotency_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS document_contents (
      document_id VARCHAR(36) NOT NULL PRIMARY KEY,
      extracted_text MEDIUMTEXT NOT NULL,
      parser_provider VARCHAR(40) NOT NULL,
      char_count INT NOT NULL,
      truncated TINYINT(1) NOT NULL DEFAULT 0,
      created_at DATETIME(6) NOT NULL,
      updated_at DATETIME(6) NOT NULL,
      CONSTRAINT fk_contents_document FOREIGN KEY (document_id) REFERENCES documents (document_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS document_chunks (
      chunk_id VARCHAR(64) NOT NULL PRIMARY KEY,
      document_id VARCHAR(36) NOT NULL,
      index_version VARCHAR(36) NOT NULL,
      parent_chunk_id VARCHAR(64) NULL,
      chunk_role VARCHAR(10) NOT NULL,
      ordinal INT NOT NULL,
      content MEDIUMTEXT NOT NULL,
      location VARCHAR(120) NULL,
      label VARCHAR(200) NOT NULL,
      created_at DATETIME(6) NOT NULL,
      KEY ix_chunks_version (index_version, chunk_role, ordinal),
      KEY ix_chunks_parent (parent_chunk_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS query_results (
      query_result_id VARCHAR(36) NOT NULL PRIMARY KEY,
      user_id VARCHAR(36) NOT NULL,
      conversation_id VARCHAR(36) NULL,
      run_id VARCHAR(36) NULL,
      data_source_id VARCHAR(40) NOT NULL,
      columns_json JSON NOT NULL,
      rows_json JSON NOT NULL,
      row_count INT NOT NULL,
      truncated TINYINT(1) NOT NULL DEFAULT 0,
      query_fingerprint CHAR(64) NOT NULL,
      tables_json JSON NOT NULL,
      duration_ms INT NOT NULL DEFAULT 0,
      created_at DATETIME(6) NOT NULL,
      expires_at DATETIME(6) NOT NULL,
      KEY ix_query_results_user (user_id, created_at),
      KEY ix_query_results_expiry (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS export_artifacts (
      artifact_id VARCHAR(36) NOT NULL PRIMARY KEY,
      approval_request_id VARCHAR(36) NOT NULL,
      query_result_id VARCHAR(36) NOT NULL,
      user_id VARCHAR(36) NOT NULL,
      format VARCHAR(10) NOT NULL,
      row_count INT NOT NULL,
      content MEDIUMTEXT NOT NULL,
      created_at DATETIME(6) NOT NULL,
      UNIQUE KEY uq_artifacts_approval (approval_request_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS demo_metadata (
      metadata_key VARCHAR(60) NOT NULL PRIMARY KEY,
      metadata_value VARCHAR(200) NOT NULL,
      updated_at DATETIME(6) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

# Expand 迁移（ADR-035）：为既有库补充执行所有权列。
# 每条语句幂等：先查 information_schema，缺列才执行 ALTER；重复启动不会报错，
# 也不会改动既有数据。新列一律可空，旧行保持 NULL（读路径语义不变）。
EXPANSION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "runs",
        "owner_token",
        "ALTER TABLE runs ADD COLUMN owner_token VARCHAR(64) NULL",
    ),
    (
        "runs",
        "heartbeat_at",
        "ALTER TABLE runs ADD COLUMN heartbeat_at DATETIME(6) NULL",
    ),
    (
        "ingestion_jobs",
        "owner_token",
        "ALTER TABLE ingestion_jobs ADD COLUMN owner_token VARCHAR(64) NULL",
    ),
    (
        "ingestion_jobs",
        "heartbeat_at",
        "ALTER TABLE ingestion_jobs ADD COLUMN heartbeat_at DATETIME(6) NULL",
    ),
)


def _column_exists(cursor, *, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    row = cursor.fetchone()
    return bool(row and int(row[0]) > 0)


def _column_length(cursor, *, table: str, column: str) -> int | None:
    cursor.execute(
        "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


# 列宽修正（ADR-036）：`document_chunks.chunk_id` / `parent_chunk_id` 原为 VARCHAR(36)，
# 而 chunk ID 形如 `<32 字符 index_version>-c<ordinal>`：ordinal 达到 3 位（>99 个 child）
# 时长度为 37，会被 MySQL 以 1406 Data too long 拒绝——长文档（P2 的目标场景）必然触发。
# 这里只做「扩宽」：仅当现有列宽小于目标值时才执行 MODIFY，重复启动无副作用，不改数据。
EXPANSION_WIDENINGS: tuple[tuple[str, str, str, int], ...] = (
    ("document_chunks", "chunk_id", "VARCHAR(64) NOT NULL", 64),
    ("document_chunks", "parent_chunk_id", "VARCHAR(64) NULL", 64),
)


def initialize_schema(settings: Settings) -> None:
    """使用应用账号创建缺失的表；绝不删除数据。"""

    pool = MySQLConnectionPool(PoolSettings.application(settings, pool_size=1))
    try:
        with pool.cursor() as cursor:
            # 逐条执行 CREATE TABLE IF NOT EXISTS：重复启动安全，只建缺失的表，不改动已有数据。
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
            # expand 迁移：只增列，不删列、不改列；存在即跳过。
            for table, column, statement in EXPANSION_COLUMNS:
                if not _column_exists(cursor, table=table, column=column):
                    cursor.execute(statement)
            # 列宽修正：只在现有列宽不足时扩宽，绝不缩小、不改语义。
            for table, column, definition, minimum in EXPANSION_WIDENINGS:
                current = _column_length(cursor, table=table, column=column)
                if current is not None and current < minimum:
                    cursor.execute(f"ALTER TABLE {table} MODIFY COLUMN {column} {definition}")
    finally:
        pool.close()


def initialize_read_only_account(settings: Settings, *, root_password: str) -> bool:
    """创建 SQL Agent 路径所用的只读账号。

    当管理凭据不可用时返回 ``False``；此时 Demo 路径改为记录手动步骤，而不是让应用启动失败。
    """

    password = settings.mysql_readonly_password.get_secret_value()
    if not password or not root_password:
        return False
    try:
        connection = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user="root",
            password=root_password,
            connect_timeout=settings.mysql_connect_timeout_seconds,
            autocommit=True,
        )
    except Exception:  # noqa: BLE001 - bootstrap must not break startup
        logger.warning("readonly_account_bootstrap_unavailable")
        return False
    try:
        user = settings.mysql_readonly_user.replace("'", "").replace("\\", "").replace("%", "")
        database = settings.readonly_database.replace("'", "").replace("\\", "").replace("%", "")
        # 密码字面量经由驱动转义，绝不作为原始字符串拼接。
        escaped_password = connection.escape(password)
        # 只读账号仅被授予目标库的 SELECT；建号与授权均幂等（IF NOT EXISTS、重复 GRANT 无副作用）。
        statements = (
            f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY {escaped_password}",
            f"GRANT SELECT ON `{database}`.* TO '{user}'@'%'",
        )
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
                connection.commit()
            cursor.execute("FLUSH PRIVILEGES")
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("readonly_account_bootstrap_failed: %s", type(error).__name__)
        return False
    finally:
        connection.close()
