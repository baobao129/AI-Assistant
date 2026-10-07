"""Index Registry: application-owned authority for active index versions (ADR-012).

Retrieval resolves the active version here, never in Milvus. Promotion and
retirement keep a last-known-good version available when a new one fails.
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
    """The requested index version cannot be served under current application state."""


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
        """Return the active version for an authorized, available Document."""

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
        """Explicit retrieval requires an active version, an available Document, and scope."""

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
        """Activate a validated version, then retire the previous active version.

        Returns ``False`` when the Document is deleted or unavailable, so a
        background task can never reactivate a deleted Document.
        """

        version = self.versions.get(index_version)
        if version is None:
            raise IndexRegistryError("index_version_not_found")
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
        """Return the matched Children plus their Parent Chunk context."""

        children = self.chunks.list_by_ids(child_ids, index_version=index_version)
        parent_ids = [
            child.parent_chunk_id for child in children if child.parent_chunk_id is not None
        ]
        parents = self.chunks.list_by_ids(parent_ids, index_version=index_version)
        return children, {parent.chunk_id: parent for parent in parents}
