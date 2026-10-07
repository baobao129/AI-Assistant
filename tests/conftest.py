"""Shared test fixtures.

Database-backed tests are opt-in through ``TEST_MYSQL_HOST`` / ``TEST_MYSQL_PORT``
so the default suite stays infrastructure-independent; run them against the Compose
runtime with the exported variables recorded in the repository handoff notes.
"""

from __future__ import annotations

import os

import pytest

from backend.infrastructure.config import Settings


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_db: requires the Compose MySQL/Milvus runtime")


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        mysql_host=os.environ.get("TEST_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.environ.get("TEST_MYSQL_PORT", "13306")),
        mysql_user=os.environ.get("TEST_MYSQL_USER", "agent_app"),
        mysql_password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
        mysql_root_password=os.environ.get("TEST_MYSQL_ROOT_PASSWORD", ""),
        mysql_readonly_user=os.environ.get("TEST_MYSQL_READONLY_USER", "agent_readonly"),
        mysql_readonly_password=os.environ.get("TEST_MYSQL_READONLY_PASSWORD", ""),
        mysql_database=os.environ.get("TEST_MYSQL_DATABASE", "enterprise_agent"),
        milvus_uri=os.environ.get("TEST_MILVUS_URI", "http://127.0.0.1:19530"),
        embedding_provider="deterministic",
        embedding_dimension=64,
        parser_provider="lightweight",
        llm_provider="mock",
        auth_bootstrap_username="demo",
        auth_bootstrap_password=os.environ.get("TEST_AUTH_PASSWORD", "test-bootstrap"),
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """Settings for database-backed tests; skips when no test credentials are exported."""

    if not os.environ.get("TEST_MYSQL_PASSWORD"):
        pytest.skip("TEST_MYSQL_PASSWORD is not exported; live database tests are skipped.")
    if not os.environ.get("TEST_MYSQL_READONLY_PASSWORD"):
        pytest.skip("TEST_MYSQL_READONLY_PASSWORD is not exported; live tests are skipped.")
    return _test_settings()
