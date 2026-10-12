"""Isolated owned-document migration, parser-free reuse and application rollback."""

import uuid

import pytest

from backend.application.runtime import build_runtime


@pytest.mark.live_db
def test_hybrid_migration_preserves_chunks_without_reparsing(live_settings, monkeypatch):
    runtime = build_runtime(live_settings)
    document_id = None
    try:
        user = runtime.auth.get_user_by_username(live_settings.auth_bootstrap_username)
        assert user is not None
        outcome = runtime.ingestion.submit_upload(
            user_id=user.user_id,
            filename=f"hybrid-migration-{uuid.uuid4().hex}.md",
            content_type="text/markdown",
            stored=runtime.upload_storage.store_bytes(
                f"采购规则 {uuid.uuid4().hex}：审批金额上限5000元。".encode()
            ),
        )
        document_id = outcome.document_id
        assert runtime.ingestion.process_job(outcome.ingestion_job_id) == "success"
        source = runtime.versions.get(outcome.index_version)
        assert source is not None

        def no_parser(*args, **kwargs):
            raise AssertionError("Migration must not parse again")

        monkeypatch.setattr(runtime.ingestion, "_extract_text", no_parser)
        migrated = runtime.ingestion.migrate_hybrid_index(
            user_id=user.user_id, document_id=document_id
        )
        assert runtime.jobs.get(migrated.ingestion_job_id).status == "success"
        assert runtime.documents.get(document_id).active_index_version == migrated.index_version
        assert runtime.versions.get(source.index_version).status == "retired"
        assert (
            runtime.chunks.count_for_version(migrated.index_version, chunk_role="child")
            == source.chunk_count
        )
        target = runtime.versions.get(migrated.index_version)
        hits = runtime.vectors.search_scope(
            collection=target.collection_name,
            embedding=[1.0] * 64,
            query="采购审批金额",
            versions=(target.index_version,),
            top_k=5,
            sparse=True,
        )
        assert hits and all(hit.document_id == document_id for hit in hits)
        runtime.versions.mark_validated(source.index_version, chunk_count=source.chunk_count)
        assert runtime.registry.promote(source.index_version)
        assert runtime.documents.get(document_id).active_index_version == source.index_version
        assert runtime.versions.get(target.index_version).status == "retired"
    finally:
        if document_id:
            deleted = runtime.ingestion.submit_delete(user_id=user.user_id, document_id=document_id)
            assert runtime.ingestion.process_job(deleted.ingestion_job_id) == "success"
        runtime.close()
