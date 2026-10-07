"""Blocking MySQL access for the application and the read-only SQL Agent path.

Two engines exist by design (``docs/contracts/TOOL_CONTRACT.md`` TOOL-003 and
``AGENTS.md`` SQL Security Rules):

* the application engine uses the configured application account for business tables;
* the read-only engine uses the configured read-only account and only executes
  validated, parameterized reads.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import pymysql

from backend.infrastructure.config import Settings

logger = logging.getLogger(__name__)

# Driver and server error codes that mean "the statement did not finish in time".
# 1205 lock wait timeout, 1317 query interrupted, 2013 socket read timeout,
# 3024 max execution time exceeded.
TIMEOUT_ERROR_CODES: frozenset[int] = frozenset({1205, 1317, 2013, 3024})

# A statement that returns at or beyond this fraction of its configured deadline is
# treated as timed out, because MySQL may answer an interrupted statement with a
# partial result rather than raising a driver error.
TIMEOUT_FRACTION = 0.9

# Driver fetch batch; the per-statement row limit is enforced on top of this.
BATCH_SIZE = 100


class DatabaseUnavailableError(RuntimeError):
    """Raised when a pooled connection cannot be produced or used."""


class _ConnectionDiscarded(Exception):
    """Internal signal: a connection is no longer safe to reuse."""


def _redact(error: BaseException) -> str:
    """Return a safe error code; driver text may embed credentials or SQL."""

    return type(error).__name__


@dataclass(frozen=True)
class PoolSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    pool_size: int
    connect_timeout: float
    read_timeout: float

    @classmethod
    def application(cls, settings: Settings, *, pool_size: int | None = None) -> PoolSettings:
        return cls(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_database,
            pool_size=pool_size or settings.mysql_pool_size,
            connect_timeout=settings.mysql_connect_timeout_seconds,
            read_timeout=max(settings.sql_query_timeout_seconds, 5.0),
        )

    @classmethod
    def read_only(cls, settings: Settings) -> PoolSettings:
        return cls(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_readonly_user,
            password=settings.mysql_readonly_password.get_secret_value(),
            database=settings.readonly_database,
            pool_size=settings.mysql_readonly_pool_size,
            connect_timeout=settings.mysql_connect_timeout_seconds,
            # The socket deadline is the hard client-side bound for one read-only
            # statement; the server-side MAX_EXECUTION_TIME is only a second guard.
            read_timeout=settings.sql_query_timeout_seconds,
        )


class MySQLConnectionPool:
    """Small bounded pool; every connection is returned or discarded, never leaked."""

    def __init__(self, settings: PoolSettings, *, autocommit: bool = True) -> None:
        self.settings = settings
        self.autocommit = autocommit
        self._available: queue.LifoQueue[pymysql.connections.Connection] = queue.LifoQueue(
            maxsize=settings.pool_size
        )
        self._lock = threading.Lock()
        self._created = 0
        for _ in range(settings.pool_size):
            connection = self._create(soft=True)
            if connection is not None:
                self._available.put_nowait(connection)

    def _create(self, *, soft: bool = False) -> pymysql.connections.Connection | None:
        try:
            connection = pymysql.connect(
                host=self.settings.host,
                port=self.settings.port,
                user=self.settings.user,
                password=self.settings.password,
                database=self.settings.database,
                connect_timeout=self.settings.connect_timeout,
                read_timeout=self.settings.read_timeout,
                autocommit=self.autocommit,
                charset="utf8mb4",
            )
        except Exception as error:  # noqa: BLE001 - driver raises several types
            logger.warning(
                "mysql_connect_failed: %s %s",
                type(error).__name__,
                error.args[0] if error.args else "",
            )
            if soft:
                return None
            raise DatabaseUnavailableError("MySQL is unavailable.") from error
        with self._lock:
            self._created += 1
        return connection

    @contextmanager
    def connection(self) -> Iterator[pymysql.connections.Connection]:
        try:
            connection = self._available.get_nowait()
        except queue.Empty:
            connection = self._create()
        try:
            yield connection
        except _ConnectionDiscarded:
            _close_quietly(connection)
            raise
        except Exception:
            _close_quietly(connection)
            raise
        else:
            try:
                if not connection.open:
                    raise DatabaseUnavailableError("MySQL connection closed.")
                self._available.put_nowait(connection)
            except queue.Full:  # pragma: no cover - pool size is fixed
                _close_quietly(connection)

    @contextmanager
    def cursor(self, *, dictionary: bool = False) -> Iterator[pymysql.cursors.Cursor]:
        with self.connection() as connection:
            cursor = connection.cursor(
                pymysql.cursors.DictCursor if dictionary else pymysql.cursors.Cursor
            )
            try:
                yield cursor
            finally:
                cursor.close()

    def discard_current_connection(self) -> None:
        """Request that the pool drop the connection used by the active context."""

        raise _ConnectionDiscarded()

    def execute(
        self,
        sql: str,
        parameters: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> int:
        with self.cursor() as cursor:
            return int(cursor.execute(sql, parameters or ()))

    def query(
        self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        with self.cursor() as cursor:
            cursor.execute(sql, parameters or ())
            return list(cursor.fetchall())

    def query_dicts(
        self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.cursor(dictionary=True) as cursor:
            cursor.execute(sql, parameters or ())
            return [dict(row) for row in cursor.fetchall()]

    def query_one(
        self, sql: str, parameters: Mapping[str, Any] | Sequence[Any] | None = None
    ) -> tuple[Any, ...] | None:
        rows = self.query(sql, parameters)
        return rows[0] if rows else None

    def close(self) -> None:
        while True:
            try:
                connection = self._available.get_nowait()
            except queue.Empty:
                return
            _close_quietly(connection)


def _close_quietly(connection: pymysql.connections.Connection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - closing must never raise
        pass


@dataclass(frozen=True)
class StatementOutcome:
    """Outcome of one validated read-only statement."""

    status: Literal["success", "timeout", "failed"]
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False
    error_code: str | None = None


class ReadOnlyQueryRunner:
    """Executes only pre-validated, parameterized reads with server-side limits."""

    def __init__(
        self, pool: MySQLConnectionPool, *, timeout_seconds: float, max_result_bytes: int
    ) -> None:
        self.pool = pool
        self.timeout_seconds = timeout_seconds
        self.max_result_bytes = max_result_bytes

    def run(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None,
        *,
        max_rows: int,
    ) -> StatementOutcome:
        started = time.monotonic()
        try:
            with self.pool.cursor() as cursor:
                self._apply_limits(cursor)
                cursor.execute(sql, parameters or {})
                elapsed = time.monotonic() - started
                if elapsed >= self.timeout_seconds * TIMEOUT_FRACTION:
                    # MySQL can answer an interrupted statement with a partial result
                    # instead of an error, so a near-deadline statement is never success.
                    self.pool.discard_current_connection()
                    return StatementOutcome(status="timeout", error_code="query_timeout")
                columns = tuple(description[0] for description in (cursor.description or ()))
                rows, truncated = self._fetch_bounded(cursor, max_rows=max_rows)
                return StatementOutcome(
                    status="success", columns=columns, rows=rows, truncated=truncated
                )
        except _ConnectionDiscarded:
            return StatementOutcome(status="timeout", error_code="query_timeout")
        except pymysql.err.OperationalError as error:
            code = error.args[0] if error.args else None
            if code in TIMEOUT_ERROR_CODES or time.monotonic() - started > self.timeout_seconds:
                # A socket deadline always leaves the connection unusable or ambiguous.
                self.pool.discard_current_connection()
                return StatementOutcome(status="timeout", error_code="query_timeout")
            return StatementOutcome(status="failed", error_code="database_error")
        except Exception:  # noqa: BLE001 - driver raises several types
            return StatementOutcome(status="failed", error_code="database_error")

    def _apply_limits(self, cursor: pymysql.cursors.Cursor) -> None:
        seconds = max(int(self.timeout_seconds), 1)
        cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={seconds * 1000}")

    def _fetch_bounded(
        self, cursor: pymysql.cursors.Cursor, *, max_rows: int
    ) -> tuple[tuple[tuple[Any, ...], ...], bool]:
        """Read at most ``max_rows`` rows and report whether more rows were available.

        The driver is asked for ``max_rows + 1`` rows so truncation is detected from the
        result set itself instead of an unbounded extra fetch.
        """

        rows: list[tuple[Any, ...]] = []
        total_bytes = 0
        truncated = False
        while True:
            batch = cursor.fetchmany(BATCH_SIZE)
            if not batch:
                break
            for row in batch:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                normalized = tuple(_json_safe(value) for value in row)
                total_bytes += sum(len(str(value)) for value in normalized)
                rows.append(normalized)
                if total_bytes >= self.max_result_bytes:
                    truncated = True
                    break
            if truncated:
                break
        return tuple(rows), truncated


def _json_safe(value: Any) -> Any:
    """Convert driver values into bounded, JSON-serializable primitives."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:2000]
    return str(value)[:2000]
