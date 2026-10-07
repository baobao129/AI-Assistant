"""Schema definition and safe bootstrap for the application database.

The DDL here is implementation detail; it does not redefine any Core Contract.
Every statement is idempotent so startup initialization can run repeatedly.
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
      chunk_id VARCHAR(36) NOT NULL PRIMARY KEY,
      document_id VARCHAR(36) NOT NULL,
      index_version VARCHAR(36) NOT NULL,
      parent_chunk_id VARCHAR(36) NULL,
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


def initialize_schema(settings: Settings) -> None:
    """Create missing tables using the application account; never drops data."""

    pool = MySQLConnectionPool(PoolSettings.application(settings, pool_size=1))
    try:
        with pool.cursor() as cursor:
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)
    finally:
        pool.close()


def initialize_read_only_account(settings: Settings, *, root_password: str) -> bool:
    """Create the read-only account used by the SQL Agent path.

    Returns ``False`` when administrative credentials are unavailable; the Demo
    path then documents the manual step instead of failing the application.
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
        # The password literal is escaped through the driver; it is never interpolated raw.
        escaped_password = connection.escape(password)
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
