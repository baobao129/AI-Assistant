"""Persistence adapters. Domain and Application layers depend on these boundaries, not on SQL."""

from backend.infrastructure.repositories.approvals import ApprovalRecord, ApprovalRepository
from backend.infrastructure.repositories.auth import AuthRepository
from backend.infrastructure.repositories.conversation import (
    ConversationRecord,
    ConversationRepository,
    MessageRecord,
    RunEventRepository,
    RunRecord,
    RunRepository,
    ThreadRepository,
)
from backend.infrastructure.repositories.documents import (
    ChunkRecord,
    ChunkRepository,
    DocumentRecord,
    DocumentRepository,
    IndexVersionRecord,
    IndexVersionRepository,
    IngestionJobRecord,
    IngestionJobRepository,
)
from backend.infrastructure.repositories.structured import (
    QueryResultRecord,
    QueryResultRepository,
)

__all__ = [
    "ApprovalRecord",
    "ApprovalRepository",
    "AuthRepository",
    "ChunkRecord",
    "ChunkRepository",
    "ConversationRecord",
    "ConversationRepository",
    "DocumentRecord",
    "DocumentRepository",
    "IndexVersionRecord",
    "IndexVersionRepository",
    "IngestionJobRecord",
    "IngestionJobRepository",
    "MessageRecord",
    "QueryResultRecord",
    "QueryResultRepository",
    "RunEventRepository",
    "RunRecord",
    "RunRepository",
    "ThreadRepository",
]
