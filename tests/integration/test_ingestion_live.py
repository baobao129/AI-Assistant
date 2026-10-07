"""摄取生命周期与启动恢复的实时集成检查（ADR-009/019）。

这些用例针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。
每个用例都使用互不相同的内容，避免内容指纹去重把不同用例耦合在一起。
"""

from __future__ import annotations

import uuid

import pytest


def document_text(label: str) -> str:
    """为每个用例生成唯一内容，使去重检测无法复用更早用例的 fixture。"""

    return (
        f"Ingestion Lifecycle Fixture {label} {uuid.uuid4().hex}\n\n"
        "Section one explains the bounded ingestion job lifecycle.\n\n"
        "Section two explains that a failed job is retried with a new job identity.\n"
    )


@pytest.fixture(scope="module")
def harness(live_settings):
    # 模块级共享一个真实容器；bootstrap Demo 用户不存在时现场创建。
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
    # 重复提交会复用当前 active 版本，而不会新增版本。
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

    # 再次 reindex 仍被接受，生成新版本，并且始终只保留一个 active 版本。
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
    # 在新 reindex 上强制制造一次有界失败，从而真实地走一遍 retry 路径。
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
    # 原 Job 作为历史保留，不会被重试覆盖。
    assert container.runtime.jobs.get(reindex.ingestion_job_id).status == "failed"
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "success"

    # 只有 failed 状态的 Job 才允许重试。
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
    # 模拟一个 worker 已 claim 该 Job 后崩溃：claim 成功但长时间不再推进。
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
    # 检索不再对外提供这个从未成功晋升过的 Document。
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version is None
    # 该 failed Job 可重试；由于尚未存有文本，重试会安全地失败而非崩溃。
    retry = container.runtime.ingestion.submit_retry(user_id=user.user_id, ingestion_job_id=job_id)
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "failed"
