"""Audit closure against real registry rows and recorded adapter boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from backend.api.dependencies import build_container
from backend.tools.contracts import ToolExecutionContext


@pytest.mark.live_db
def test_explicit_versions_reject_before_any_provider_and_recheck_scope(live_settings, monkeypatch):
    container = build_container(live_settings)
    runtime = container.runtime
    user = runtime.auth.get_user_by_username(live_settings.auth_bootstrap_username)
    other = runtime.auth.create_user(
        username=f"audit-other-{uuid4().hex[:12]}",
        password=uuid4().hex,
        display_name="Audit fixture",
    )
    created = []
    observations = []

    def upload(owner):
        item = runtime.ingestion.submit_upload(
            user_id=owner,
            filename="audit.md",
            content_type="text/markdown",
            stored=runtime.upload_storage.store_bytes(
                f"采购审批上限5000元。审计夹具 {uuid4().hex}".encode()
            ),
        )
        created.append((owner, item.document_id))
        assert runtime.ingestion.process_job(item.ingestion_job_id) == "success"
        return item

    try:
        own, foreign, removed = upload(user.user_id), upload(other.user_id), upload(user.user_id)
        assert runtime.documents.mark_deleted(removed.document_id, user_id=user.user_id)
        prior = own.index_version
        reindex = runtime.ingestion.submit_reindex(
            user_id=user.user_id, document_id=own.document_id
        )
        assert runtime.ingestion.process_job(reindex.ingestion_job_id) == "success"
        tool = container.tools.registry.get("knowledge_search")
        counts = {"embedding": 0, "dense": 0, "bm25": 0, "rerank": 0}
        real_embed, real_search = tool.embeddings.embed, tool.vectors.search_scope

        def embed(text):
            counts["embedding"] += 1
            return real_embed(text)

        def search(**kwargs):
            counts["bm25" if kwargs.get("sparse") else "dense"] += 1
            return real_search(**kwargs)

        def rerank(*args, **kwargs):
            counts["rerank"] += 1
            raise AssertionError("Rejected requests must not reach rerank")

        monkeypatch.setattr(tool.embeddings, "embed", embed)
        monkeypatch.setattr(tool.vectors, "search_scope", search)
        monkeypatch.setattr(tool.reranker, "rerank", rerank)
        context = ToolExecutionContext(
            user_id=user.user_id, authorized_document_ids=(own.document_id,)
        )

        def rejected(name, version, expected="index_version_not_active", document_ids=None):
            before = dict(counts)
            args = {"query": "采购审批上限", "index_version": version}
            if document_ids is not None:
                args["document_ids"] = document_ids
            result = container.tools.invoke("knowledge_search", uuid4().hex, args, context)
            assert result.status == "rejected", name
            assert result.error.code == expected, name
            assert counts == before, name
            observations.append(
                {
                    "case": name,
                    "error_code": result.error.code,
                    "provider_delta": {k: counts[k] - before[k] for k in counts},
                }
            )

        rejected("unknown", uuid4().hex)
        rejected("foreign", foreign.index_version)
        rejected("deleted", removed.index_version)
        rejected("retired", prior)
        rejected(
            "scope_conflict",
            reindex.index_version,
            "document_scope_unavailable",
            [foreign.document_id],
        )
        positive = container.tools.invoke(
            "knowledge_search",
            uuid4().hex,
            {"query": "采购审批上限", "index_version": reindex.index_version},
            context,
        )
        assert positive.status == "success" and positive.data["results"]
        assert counts["embedding"] == 1 and counts["dense"] == 1 and counts["bm25"] == 1
        # A residual active flag must not authorize a version which is not the document pointer.
        runtime.application_pool.execute(
            "UPDATE document_index_versions SET status = 'active' WHERE index_version = %s",
            (prior,),
        )
        rejected("active_flag_but_wrong_pointer", prior)
        # Authorized when Run starts, deleted before retrieval: execution boundary fails closed.
        conversation = runtime.conversations.create(user_id=user.user_id, title="scope race")
        handle = container.runs.start_run(
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            user_input="采购制度",
            thread_id=None,
            document_ids=[own.document_id],
        )
        assert runtime.documents.mark_deleted(own.document_id, user_id=user.user_id)
        rejected(
            "deleted_after_start",
            reindex.index_version,
            "document_scope_unavailable",
            list(handle.document_scope),
        )
    finally:
        output = os.environ.get("AUDIT_EVIDENCE_DIR")
        if output:
            Path(output, "provider-call-counters.json").write_text(
                json.dumps(
                    {
                        "cases": observations,
                        "adapters": (
                            "real MySQL registry and deterministic embedding / real Milvus; "
                            "rerank recorded guard, no remote quality claim"
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        for owner, document in created:
            current = runtime.documents.get(document, user_id=owner)
            if current is not None and current.status != "deleted":
                deletion = runtime.ingestion.submit_delete(user_id=owner, document_id=document)
                assert runtime.ingestion.process_job(deletion.ingestion_job_id) == "success"
        container.close()
