"""面向应用与只读 SQL Agent 路径的阻塞式 MySQL 访问。

按设计存在两个 engine（见 ``docs/contracts/TOOL_CONTRACT.md`` 的 TOOL-003 与
``AGENTS.md`` 的 SQL Security Rules）：

* 应用 engine 使用配置的应用账号访问业务表；
* 只读 engine 使用配置的只读账号，仅执行已校验、参数化的读取。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import pymysql

from backend.infrastructure.config import Settings

logger = logging.getLogger(__name__)

# 表示「语句未在时限内完成」的驱动与服务器错误码：
# 1205 锁等待超时、1317 查询被中断、2013 socket 读超时、3024 超过最大执行时间。
TIMEOUT_ERROR_CODES: frozenset[int] = frozenset({1205, 1317, 2013, 3024})

# 语句在接近或超过配置时限的该比例后才返回时，即视为超时；因为 MySQL 对被中断的
# 语句可能返回部分结果，而不是抛出驱动错误。
TIMEOUT_FRACTION = 0.9

# 驱动抓取批次大小；每语句的行数上限在此之上再做限制。
BATCH_SIZE = 100


class DatabaseUnavailableError(RuntimeError):
    """当无法从连接池获取或使用连接时抛出。"""


class _ConnectionDiscarded(Exception):
    """内部信号：某连接已不再安全、不可复用。"""


def _redact(error: BaseException) -> str:
    """返回安全的错误码；驱动文本可能夹带凭据或 SQL。"""

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
            # socket 期限就是只读单条语句在客户端侧的硬约束；
            # 服务端的 MAX_EXECUTION_TIME 只是第二道保险。
            read_timeout=settings.sql_query_timeout_seconds,
        )


class MySQLConnectionPool:
    """小型有界连接池；每个连接要么归还、要么丢弃，绝不泄露。"""

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
    def connection(self) -> Generator[pymysql.connections.Connection, None, None]:
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
    def cursor(self, *, dictionary: bool = False) -> Generator[pymysql.cursors.Cursor, None, None]:
        with self.connection() as connection:
            cursor = connection.cursor(
                pymysql.cursors.DictCursor if dictionary else pymysql.cursors.Cursor
            )
            try:
                yield cursor
            finally:
                cursor.close()

    @contextmanager
    def transaction(self) -> Generator[pymysql.cursors.Cursor, None, None]:
        """在一个应用账号连接上执行原子事务：正常退出提交，异常回滚。

        HITL 决策与其 successor Run 的创建必须共享同一个事务边界：进程在任一步骤
        退出时整体回滚，绝不留下「approved 但没有 successor」的不可恢复中间态。
        连接上的 autocommit 设置会在事务结束后恢复。
        """

        with self.connection() as connection:
            previous_autocommit = connection.get_autocommit()
            connection.autocommit(False)
            cursor = connection.cursor()
            try:
                yield cursor
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                cursor.close()
                connection.autocommit(previous_autocommit)

    def discard_current_connection(self) -> None:
        """请求连接池丢弃当前上下文正在使用的连接。"""

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
    """一条已校验只读语句的执行结果。"""

    status: Literal["success", "timeout", "failed"]
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False
    error_code: str | None = None


class ReadOnlyQueryRunner:
    """仅执行预先校验、参数化的读取，并施加服务端限制。"""

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
                    # MySQL 对被中断的语句可能返回部分结果而非报错，
                    # 因此接近时限的语句一律不算成功。
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
                # socket 期限一到，连接要么不可用、要么状态不确定。
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
        """最多读取 ``max_rows`` 行，并报告是否还有更多可用行。

        向驱动索取 ``max_rows + 1`` 行，从而直接由结果集判断是否截断，避免无边界的额外抓取。
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
    """将驱动返回值转换为有界、可 JSON 序列化的基本类型。"""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:2000]
    return str(value)[:2000]
