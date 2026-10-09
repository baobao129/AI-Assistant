from __future__ import annotations

from backend.infrastructure.vector_store import MilvusVectorStore, VectorRecord


class _Client:
    def __init__(self) -> None:
        self.rows = []

    def upsert(self, *, collection_name: str, data: list[dict], timeout: float) -> dict:
        self.rows = data
        return {"upsert_count": len(data)}


def test_upsert_truncates_multibyte_previews_by_utf8_byte_limit() -> None:
    store = MilvusVectorStore(uri="http://unused")
    client = _Client()
    store._client = client  # type: ignore[assignment]
    record = VectorRecord(
        chunk_id="chunk-1",
        document_id="document-1",
        index_version="version-1",
        parent_chunk_id=None,
        content="内容" * 3000,
        label="章节" * 300,
        location="位置" * 200,
        embedding=[0.1, 0.2],
    )

    assert store.upsert(collection="test", records=[record]) == 1

    row = client.rows[0]
    assert len(row["content"].encode("utf-8")) <= 8192
    assert len(row["label"].encode("utf-8")) <= 512
    assert len(row["location"].encode("utf-8")) <= 256
    assert row["label"].encode("utf-8").decode("utf-8") == row["label"]
