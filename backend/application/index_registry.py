"""Index Registry：由 application 持有的 active Index Version 权威来源（ADR-012）。

检索在此处解析 active version，绝不在 Milvus 中解析。promote 与 retire
会在新版本失败时保留 last-known-good 版本可用。
"""

from __future__ import annotations

import logging

from backend.infrastructure.repositories.documents import (
    DOCUMENT_STATUS_DELETED,
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
        # A historical row's active flag cannot override the document pointer.
        # Interrupted promote/retire transitions can otherwise authorize stale versions.
        if document.active_index_version != version.index_version:
            raise IndexRegistryError("index_version_not_active")
        return version.index_version

    def promote(self, index_version: str) -> bool:
        """激活一个已校验的版本，然后 retire 之前的 active version。

        当 Document 已删除或不可用时返回 ``False``，因此后台任务
        绝不可能重新激活一个已删除的 Document。
        """

        version = self.versions.get(index_version)
        if version is None:
            raise IndexRegistryError("index_version_not_found")
        document = self.documents.get(version.document_id)
        # promote 之前复检 Document：删除与 promote 可能并发，已删除的文档绝不能被重新激活。
        # 注意这里**不能**要求文档已经是 available —— indexing -> available 正是 promote
        # 要做的转换（ADR-040 决策 2）；竞态由下面那条条件 UPDATE 自己兜住。
        if document is None or document.status == DOCUMENT_STATUS_DELETED:
            logger.warning("promotion_refused_document_unavailable")
            self.versions.mark_failed(index_version, failure_code="document_not_available")
            return False
        prior_active = document.active_index_version
        # 状态与 active 版本在同一条条件 UPDATE 中写入：既保证二者一致，
        # 也保证在并发删除时 promote 无副作用地失败。
        if not self.documents.activate(version.document_id, index_version):
            self.versions.mark_failed(index_version, failure_code="document_not_available")
            return False
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


class RegistryParentContext:
    """Graph 侧「父块提供者」的实现（ADR-038 决策 6）。

    Graph 节点不接触仓储类型或数据库：这里把 application-owned 的父块元数据映射为
    **有界的普通字典**，并按 parent chunk ID 去重、按角色与 index version 过滤、
    按 ``max_parents`` 截断。过滤条件都在服务端判定，绝不因为「检索命中了一个 ID」
    就信任它指向的是本版本的父块。
    """

    def __init__(self, *, registry: IndexRegistry, max_parents: int) -> None:
        self.registry = registry
        self.max_parents = max(1, max_parents)

    def parents_for(
        self, *, index_version: str, child_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        if not index_version or not child_ids:
            return {}
        _, parents = self.registry.chunks_for_context(
            index_version=index_version, child_ids=list(child_ids)
        )
        context: dict[str, dict[str, object]] = {}
        for parent in parents.values():
            # 只接受同一 index version 的 parent 角色记录：跨版本或非父块一律丢弃。
            if parent.chunk_role != "parent" or parent.index_version != index_version:
                continue
            if parent.chunk_id in context:
                continue
            context[parent.chunk_id] = {
                "chunk_id": parent.chunk_id,
                "content": parent.content,
                "label": parent.label,
                "location": parent.location,
                "section_path": parent.section_path,
                "block_type": parent.block_type,
                "page_start": parent.page_start,
                "page_end": parent.page_end,
                "token_count": parent.token_count,
            }
            if len(context) >= self.max_parents:
                break
        return context
