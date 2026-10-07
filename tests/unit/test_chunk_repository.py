"""ChunkRepository 写入边界的显式约束测试。

``document_chunks.location`` 列是 VARCHAR(120)。此前只有在「当前定位串恰好很短」
这一隐含假设下才不会出问题；现在写入边界显式截断，本测试用记录型连接池桩验证该约束。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from backend.infrastructure.repositories.documents import (
    MAX_LOCATION_CHARS,
    ChunkRecord,
    ChunkRepository,
)


class RecordingCursor:
    """记录 executemany 参数的游标桩。"""

    def __init__(self) -> None:
        self.executions: list[tuple[str, list[tuple[object, ...]]]] = []

    def executemany(self, sql: str, parameters) -> None:
        self.executions.append((sql, [tuple(row) for row in parameters]))


class RecordingPool:
    """只提供仓储所需 cursor() 上下文的连接池桩。"""

    def __init__(self) -> None:
        self.recorded = RecordingCursor()

    @contextmanager
    def cursor(self) -> Iterator[RecordingCursor]:
        yield self.recorded


def _chunk(location: str | None) -> ChunkRecord:
    return ChunkRecord(
        chunk_id="v1-c1",
        document_id="doc-1",
        index_version="v1",
        parent_chunk_id="v1-p0",
        chunk_role="child",
        ordinal=1,
        content="内容",
        location=location,
        label="标签",
    )


def test_over_long_location_is_explicitly_bounded_to_the_column_width():
    pool = RecordingPool()
    ChunkRepository(pool).add_many([_chunk("L" * 300)])

    _, rows = pool.recorded.executions[0]
    location = rows[0][7]
    assert location == "L" * MAX_LOCATION_CHARS


def test_short_and_null_locations_are_unchanged():
    pool = RecordingPool()
    ChunkRepository(pool).add_many([_chunk("chunk-7"), _chunk(None)])

    _, rows = pool.recorded.executions[0]
    assert rows[0][7] == "chunk-7"
    assert rows[1][7] is None
