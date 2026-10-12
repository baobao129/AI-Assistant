"""Real MySQL UTC cursor regression, restricted to a dedicated empty audit DB."""

from datetime import datetime

import pytest

from backend.infrastructure.database import MySQLConnectionPool, PoolSettings
from backend.infrastructure.repositories.auth import AuthRepository
from backend.infrastructure.repositories.conversation import ConversationRepository
from backend.infrastructure.repositories.documents import DocumentRepository


@pytest.mark.live_db
@pytest.mark.parametrize("session_timezone", ["+00:00", "+08:00", "-05:00"])
@pytest.mark.parametrize("kind", ["conversations", "messages", "documents"])
def test_next_page_uses_utc_even_in_a_non_utc_mysql_session(
    live_settings, session_timezone: str, kind: str
):
    # Refuse shared/demo databases even when TEST_MYSQL_* is configured.
    if not live_settings.mysql_database.startswith("cursor_audit_"):
        pytest.skip("Requires a dedicated cursor_audit_* database in an isolated Compose project")
    pool = MySQLConnectionPool(PoolSettings.application(live_settings, pool_size=1))
    conversations = ConversationRepository(pool)
    documents = DocumentRepository(pool)
    user = AuthRepository(pool).create_user(
        username=f"cursor-{kind}-{session_timezone}",
        password=live_settings.auth_bootstrap_password.get_secret_value(),
        display_name="Isolated cursor fixture",
    )
    times = [
        datetime(2026, 1, 1, 12, 34, 56, microsecond) for microsecond in (123456, 123455, 123454)
    ]
    identities = []
    try:
        pool.execute("SET SESSION time_zone = %s", (session_timezone,))
        parent = conversations.create(user_id=user.user_id, title="message fixture")
        for index, created in enumerate(times):
            if kind == "documents":
                row = documents.create(
                    user_id=user.user_id,
                    title="cursor fixture",
                    source_name=f"fixture-{index}.md",
                    content_type="text/markdown",
                    content_fingerprint=str(index) * 64,
                    byte_size=10,
                )
                identity = row.document_id
                pool.execute(
                    "UPDATE documents SET created_at = %s WHERE document_id = %s",
                    (created, identity),
                )
            elif kind == "messages":
                row = conversations.add_message(
                    conversation_id=parent.conversation_id,
                    role="user",
                    content="fixture",
                    status="completed",
                )
                identity = row.message_id
                pool.execute(
                    "UPDATE conversation_messages SET created_at = %s WHERE message_id = %s",
                    (created, identity),
                )
            else:
                row = conversations.create(user_id=user.user_id, title="cursor fixture")
                identity = row.conversation_id
                pool.execute(
                    "UPDATE conversations SET created_at = %s WHERE conversation_id = %s",
                    (created, identity),
                )
            identities.append(identity)
        if kind == "documents":
            method = documents.list
            scope = {"user_id": user.user_id}
            id_field = "document_id"
        elif kind == "messages":
            method = conversations.list_messages
            scope = {"conversation_id": parent.conversation_id}
            id_field = "message_id"
        else:
            # The parent fixture must not appear in the conversation listing.
            conversations.archive(parent.conversation_id, user_id=user.user_id)
            method = conversations.list
            scope = {"user_id": user.user_id}
            id_field = "conversation_id"
        first, cursor = method(**scope, limit=1, cursor=None)
        assert cursor == "1767270896123456"
        seen = [getattr(first[0], id_field)]
        for _ in range(2):
            page, cursor = method(**scope, limit=1, cursor=cursor)
            assert len(page) == 1
            seen.append(getattr(page[0], id_field))
        assert seen == identities
        assert cursor is None
    finally:
        # This account and all its rows were created in this test only.
        pool.execute(
            "DELETE FROM conversation_messages WHERE conversation_id IN "
            "(SELECT conversation_id FROM conversations WHERE user_id = %s)",
            (user.user_id,),
        )
        pool.execute("DELETE FROM documents WHERE user_id = %s", (user.user_id,))
        pool.execute("DELETE FROM conversations WHERE user_id = %s", (user.user_id,))
        pool.execute("DELETE FROM users WHERE user_id = %s", (user.user_id,))
        pool.close()
