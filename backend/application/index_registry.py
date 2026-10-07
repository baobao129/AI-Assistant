"""Index Registry：由 application 持有的 active Index Version 权威来源（ADR-012）。

检索在此处解析 active version，绝不在 Milvus 中解析。promote 与 retire
会在新版本失败时保留 last-known-good 版本可用。
"""

from __future__ import annotations

import logging

from backend.infrastructure.repositories.documents import (
    ChunkRecord,
    ChunkRepository,
    DocumentRepository,
    IndexVersionRepository,
)

logger = logging.getLogger(__name__)


class IndexRegistryError(RuntimeError):
    """在当前 application 状态下无法提供所请求的 index version。"""


class IndexRegistry:
    def __init__(
        self,
        *,
        documents: DocumentRepository,
        versions: IndexVersionRepository,
        chunks: ChunkRepository,
    ) -> None:
        self.documents = documents
        self.versions = versions
        self.chunks = chunks

    def resolve_active_version(self, document_id: str, *, user_id: str) -> str | None:
        """返回某个已授权且可用 Document 的 active version。"""

        document = self.documents.get(document_id, user_id=user_id)
        if document is None or document.status != "available":
            return None
        if not document.active_index_version:
            return None
        version = self.versions.get(document.active_index_version)
        if version is None or version.status != "active":
            return None
        return version.index_version

    def resolve_explicit_version(self, index_version: str, *, user_id: str) -> str:
        """显式检索要求 active version、可用的 Document，以及资源范围。"""

        version = self.versions.get(index_version)
        if version is None:
            raise IndexRegistryError("index_version_not_found")
        if version.status != "active":
            raise IndexRegistryError("index_version_not_active")
        document = self.documents.get(version.document_id, user_id=user_id)
        if document is None or document.status != "available":
            raise IndexRegistryError("document_not_available")
        return version.index_version

    def promote(self, index_version: str) -> bool:
        """激活一个已校验的版本，然后 retire 之前的 active version。

        当 Document 已删除或不可用时返回 ``False``，因此后台任务
        绝不可能重新激活一个已删除的 Document。
        """

        version = self.versions.get(index_version)
        if version is None:
            raise IndexRegistryError("index_version_not_found")
        # promote 之前复检 Document 可用性：删除与 promote 可能并发，
        # 这一步保证已删除的 Document 不会被重新激活。
        if not self.documents.is_available(version.document_id):
            logger.warning("promotion_refused_document_unavailable")
            self.versions.mark_failed(index_version, failure_code="document_not_available")
            return False
        previous = self.documents.get(version.document_id)
        if not self.documents.is_available(version.document_id):
            self.versions.mark_failed(index_version, failure_code="document_not_available")
            return False
        prior_active = previous.active_index_version if previous else None
        self.documents.set_active_version(version.document_id, index_version)
        if prior_active and prior_active != index_version:
            self.versions.retire(prior_active)
        return True

    def chunks_for_context(
        self, *, index_version: str, child_ids: list[str]
    ) -> tuple[list[ChunkRecord], dict[str, ChunkRecord]]:
        """返回命中的 Children 及其 Parent Chunk 上下文。"""

        children = self.chunks.list_by_ids(child_ids, index_version=index_version)
        parent_ids = [
            child.parent_chunk_id for child in children if child.parent_chunk_id is not None
        ]
        parents = self.chunks.list_by_ids(parent_ids, index_version=index_version)
        return children, {parent.chunk_id: parent for parent in parents}
