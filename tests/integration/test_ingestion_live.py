"""Live integration checks for the ingestion lifecycle and startup recovery (ADR-009/019).

These run against the configured application database and are skipped unless live
credentials are exported (see ``tests/conftest.py``). Each test uses distinct content so
content-fingerprint deduplication cannot couple the cases.
"""

from __future__ import annotations

import uuid

import pytest


def document_text(label: str) -> str:
    """Unique content per test so duplicate detection cannot reuse an earlier fixture."""

    return (
        f"Ingestion Lifecycle Fixture {label} {uuid.uuid4().hex}\n\n"
        "Section one explains the bounded ingestion job lifecycle.\n\n"
        "Section two explains that a failed job is retried with a new job identity.\n"
    )


@pytest.fixture(scope="module")
def harness(live_settings):
    from backend.api.dependencies import build_container

    settings = live_settings
    container = build_container(settings)
    user = container.runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    if user is None:
        user = container.runtime.auth.create_user(
            username=settings.auth_bootstrap_username,
            password=settings.auth_bootstrap_password.get_secret_value(),
            display_name=settings.auth_bootstrap_display_name,
        )
    yield {"container": container, "user": user, "settings": settings}
    container.close()


def _upload(container, user, *, label: str):
    text = document_text(label)
    payload = text.encode("utf-8")
    outcome = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename=f"{label}-{uuid.uuid4().hex[:8]}.md",
        content_type="text/markdown",
        data=payload,
    )
    assert outcome.duplicate is False, "fixture content must be unique"
    return outcome, payload


@pytest.mark.live_db
def test_upload_indexes_and_promotes_the_active_version(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, payload = _upload(container, user, label="promote")
    assert (
        container.runtime.ingestion.process_job(outcome.ingestion_job_id, payload=payload)
        == "success"
    )
    document = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert document is not None
    assert document.active_index_version == outcome.index_version
    version = container.runtime.versions.get(outcome.index_version)
    assert version is not None and version.status == "active" and version.chunk_count > 0
    assert (
        container.runtime.registry.resolve_active_version(outcome.document_id, user_id=user.user_id)
        == outcome.index_version
    )


@pytest.mark.live_db
def test_duplicate_upload_reuses_the_active_document(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, payload = _upload(container, user, label="duplicate")
    assert (
        container.runtime.ingestion.process_job(outcome.ingestion_job_id, payload=payload)
        == "success"
    )

    second = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename="duplicate-again.md",
        content_type="text/markdown",
        data=payload,
    )
    assert second.duplicate is True
    assert second.document_id == outcome.document_id
    document = container.runtime.documents.get(second.document_id, user_id=user.user_id)
    assert document is not None and document.status == "available"
    # A duplicate submission reuses the active version and does not add a new one.
    assert second.index_version == document.active_index_version
    versions = container.runtime.versions.list_for_document(document.document_id)
    assert len(versions) == 1
    assert versions[0].status == "active"


@pytest.mark.live_db
def test_reindex_promotes_a_new_version_and_retires_the_old_one(harness):
    container = harness["container"]
    user = harness["user"]
    upload, payload = _upload(container, user, label="reindex")
    assert (
        container.runtime.ingestion.process_job(upload.ingestion_job_id, payload=payload)
        == "success"
    )
    first_version = upload.index_version

    reindex = container.runtime.ingestion.submit_reindex(
        user_id=user.user_id, document_id=upload.document_id
    )
    assert reindex.index_version != first_version
    assert container.runtime.ingestion.process_job(reindex.ingestion_job_id) == "success"

    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version == reindex.index_version
    retired = container.runtime.versions.get(first_version)
    assert retired is not None and retired.status == "retired"
    versions = container.runtime.versions.list_for_document(upload.document_id)
    active = [version for version in versions if version.status == "active"]
    assert len(active) == 1
    assert active[0].index_version == reindex.index_version

    # A further reindex is accepted, creates a new version, and still leaves one active.
    third = container.runtime.ingestion.submit_reindex(
        user_id=user.user_id, document_id=upload.document_id
    )
    assert third.index_version not in {first_version, reindex.index_version}
    assert container.runtime.ingestion.process_job(third.ingestion_job_id) == "success"
    versions = container.runtime.versions.list_for_document(upload.document_id)
    active = [version for version in versions if version.status == "active"]
    assert len(active) == 1
    assert active[0].index_version == third.index_version


@pytest.mark.live_db
def test_failed_job_retry_creates_a_new_job_with_incremented_attempt(harness):
    from backend.domain.errors import AppError
    from backend.infrastructure.repositories.documents import JOB_STATUS_FAILED

    container = harness["container"]
    user = harness["user"]
    upload, payload = _upload(container, user, label="retry")
    assert (
        container.runtime.ingestion.process_job(upload.ingestion_job_id, payload=payload)
        == "success"
    )
    # Force a bounded failure on a fresh reindex so the retry path is exercised for real.
    reindex = container.runtime.ingestion.submit_reindex(
        user_id=user.user_id, document_id=upload.document_id
    )
    container.runtime.jobs.finish(
        reindex.ingestion_job_id,
        status=JOB_STATUS_FAILED,
        failure_code="test_forced_failure",
        failure_message="Forced failure for retry coverage.",
    )
    first = container.runtime.jobs.get(reindex.ingestion_job_id)
    assert first is not None and first.status == "failed"

    retry = container.runtime.ingestion.submit_retry(
        user_id=user.user_id, ingestion_job_id=reindex.ingestion_job_id
    )
    assert retry.ingestion_job_id != reindex.ingestion_job_id
    second = container.runtime.jobs.get(retry.ingestion_job_id)
    assert second is not None
    assert second.attempt == first.attempt + 1
    # The original remains history and is not overwritten.
    assert container.runtime.jobs.get(reindex.ingestion_job_id).status == "failed"
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "success"

    # Only failed Jobs are retryable.
    with pytest.raises(AppError):
        container.runtime.ingestion.submit_retry(
            user_id=user.user_id, ingestion_job_id=retry.ingestion_job_id
        )


@pytest.mark.live_db
def test_startup_recovery_fails_stale_running_jobs(harness):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(container, user, label="recovery")
    job_id = upload.ingestion_job_id
    # Simulate a worker that claimed the Job and then died.
    assert container.runtime.jobs.claim(job_id) is True
    container.runtime.jobs.pool.execute(
        "UPDATE ingestion_jobs SET updated_at = %s, created_at = %s WHERE ingestion_job_id = %s",
        (utc_now() - timedelta(hours=2), utc_now() - timedelta(hours=2), job_id),
    )
    recovered = container.runtime.ingestion.recover_stale_jobs()
    assert recovered["stale_running"] >= 1
    job = container.runtime.jobs.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.failure_code == "stale_running_job"
    # Retrieval no longer offers a Document that was never promoted.
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version is None
    # The failed Job is retryable and, with no stored text yet, fails safely.
    retry = container.runtime.ingestion.submit_retry(user_id=user.user_id, ingestion_job_id=job_id)
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "failed"
