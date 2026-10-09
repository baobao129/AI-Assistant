"""Vector Store 适配器：Milvus Standalone 保存 Child Chunk 向量（ADR-006/ADR-012）。

Milvus 是向量执行存储。活跃的 index-version 权威在应用 registry 中；本适配器绝不判定
哪个版本是活跃的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from pymilvus import DataType, MilvusClient

from backend.infrastructure.config import Settings

logger = logging.getLogger(__name__)

# content/label/location 等仅作元数据预览写入 Milvus；写入前截断以适配 VARCHAR 列宽。
CONTENT_PREVIEW_CHARS = 2000


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """截断到 Milvus VARCHAR 的 UTF-8 字节上限，且不切断多字节字符。"""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class VectorStoreError(RuntimeError):
    """Milvus 无法完成所请求的向量操作。"""


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: str
    document_id: str
    index_version: str
    parent_chunk_id: str | None
    content: str
    label: str
    location: str | None
    embedding: list[float]


@dataclass(frozen=True)
class VectorMatch:
    chunk_id: str
    document_id: str
    index_version: str
    parent_chunk_id: str | None
    score: float


class VectorStore(Protocol):
    def ensure_collection(self, *, name: str, dimension: int) -> None: ...

    def upsert(self, *, collection: str, records: list[VectorRecord]) -> int: ...

    def search(
        self,
        *,
        collection: str,
        embedding: list[float],
        top_k: int,
        document_ids: tuple[str, ...] = (),
        index_version: str | None = None,
    ) -> list[VectorMatch]: ...

    def delete_version(self, *, collection: str, index_version: str) -> int: ...


class MilvusVectorStore:
    provider_name = "milvus"

    def __init__(self, *, uri: str, timeout_seconds: float = 10.0) -> None:
        self.uri = uri
        self.timeout_seconds = timeout_seconds
        self._client: MilvusClient | None = None

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            self._client = MilvusClient(uri=self.uri, timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def ensure_collection(self, *, name: str, dimension: int) -> None:
        try:
            if self.client.has_collection(name, timeout=self.timeout_seconds):
                return
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("index_version", DataType.VARCHAR, max_length=64)
            schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=64)
            schema.add_field("content", DataType.VARCHAR, max_length=8192)
            schema.add_field("label", DataType.VARCHAR, max_length=512)
            schema.add_field("location", DataType.VARCHAR, max_length=256)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dimension)
            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE"
            )
            index_params.add_index(field_name="index_version", index_type="INVERTED")
            index_params.add_index(field_name="document_id", index_type="INVERTED")
            self.client.create_collection(
                collection_name=name,
                schema=schema,
                index_params=index_params,
                timeout=self.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - SDK raises several types
            logger.warning("vector_collection_failed: %s", type(error).__name__)
            raise VectorStoreError("vector_store_unavailable") from error

    def upsert(self, *, collection: str, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        # 仅写入元数据预览：Milvus VARCHAR 的 max_length 按 UTF-8 字节计数。
        rows = [
            {
                "chunk_id": record.chunk_id,
                "document_id": record.document_id,
                "index_version": record.index_version,
                "parent_chunk_id": record.parent_chunk_id or "",
                "content": _truncate_utf8(record.content, 8192),
                "label": _truncate_utf8(record.label, 512),
                "location": _truncate_utf8(record.location or "", 256),
                "embedding": record.embedding,
            }
            for record in records
        ]
        try:
            result = self.client.upsert(
                collection_name=collection, data=rows, timeout=self.timeout_seconds
            )
            return (
                int(result.get("upsert_count", len(rows)))
                if isinstance(result, dict)
                else len(rows)
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("vector_upsert_failed: %s", type(error).__name__)
            raise VectorStoreError("vector_store_write_failed") from error

    def search(
        self,
        *,
        collection: str,
        embedding: list[float],
        top_k: int,
        document_ids: tuple[str, ...] = (),
        index_version: str | None = None,
    ) -> list[VectorMatch]:
        clauses = []
        if document_ids:
            quoted = ", ".join(f'"{document_id}"' for document_id in document_ids)
            clauses.append(f"document_id in [{quoted}]")
        if index_version:
            clauses.append(f'index_version == "{index_version}"')
        expression = " and ".join(clauses) if clauses else None
        try:
            raw = self.client.search(
                collection_name=collection,
                data=[embedding],
                limit=max(1, top_k),
                output_fields=["chunk_id", "document_id", "index_version", "parent_chunk_id"],
                filter=expression,
                search_params={"metric_type": "COSINE"},
                timeout=self.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("vector_search_failed: %s", type(error).__name__)
            raise VectorStoreError("vector_store_unavailable") from error
        matches: list[VectorMatch] = []
        for hit in raw[0] if raw else []:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            matches.append(
                VectorMatch(
                    chunk_id=str(entity.get("chunk_id") or hit.get("id", "")),
                    document_id=str(entity.get("document_id", "")),
                    index_version=str(entity.get("index_version", "")),
                    parent_chunk_id=str(entity.get("parent_chunk_id") or "") or None,
                    score=float(hit.get("distance", 0.0)),
                )
            )
        return matches

    def delete_version(self, *, collection: str, index_version: str) -> int:
        try:
            if not self.client.has_collection(collection, timeout=self.timeout_seconds):
                return 0
            result = self.client.delete(
                collection_name=collection,
                filter=f'index_version == "{index_version}"',
                timeout=self.timeout_seconds,
            )
            if isinstance(result, dict):
                return int(result.get("delete_count", 0))
            return 0
        except Exception as error:  # noqa: BLE001
            logger.warning("vector_delete_failed: %s", type(error).__name__)
            raise VectorStoreError("vector_store_write_failed") from error

    def health(self) -> bool:
        try:
            self.client.list_collections(timeout=self.timeout_seconds)
            return True
        except Exception:  # noqa: BLE001
            return False


def collection_name(*, provider: str, model: str, dimension: int) -> str:
    """collection 标识与 embedding model 和维度绑定（PROJECT_SPEC §8）。"""

    # 命名规则：knowledge_<provider>_<model>_<dimension>；非字母数字字符替换为下划线，
    # 并截断到 120 字符以内，避免超出 Milvus collection 名称上限。
    safe_model = "".join(character if character.isalnum() else "_" for character in model)
    return f"knowledge_{provider}_{safe_model}_{dimension}"[:120]


def create_vector_store(settings: Settings) -> MilvusVectorStore:
    return MilvusVectorStore(
        uri=settings.milvus_uri, timeout_seconds=settings.milvus_timeout_seconds
    )
