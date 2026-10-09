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
    created: list[str] = []
    yield {"container": container, "user": user, "settings": settings, "created": created}
    # 自清理：删除本模块创建的夹具文档并清理其向量，避免反复运行验收把共享演示语料撑大
    # （语料膨胀会挤占检索 top_k，使不相关的验收断言偶发失败）。
    for document_id in created:
        try:
            outcome = container.runtime.ingestion.submit_delete(
                user_id=user.user_id, document_id=document_id
            )
            container.runtime.ingestion.process_job(outcome.ingestion_job_id)
        except Exception as error:  # noqa: BLE001 - 清理失败不得影响测试结论
            print(f"ingestion_fixture_cleanup_failed: {document_id[:8]} {type(error).__name__}")
    container.close()


def _upload(harness, *, label: str):
    container = harness["container"]
    user = harness["user"]
    text = document_text(label)
    payload = text.encode("utf-8")
    outcome = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename=f"{label}-{uuid.uuid4().hex[:8]}.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(payload),
    )
    assert outcome.duplicate is False, "fixture content must be unique"
    # 登记以便模块结束时统一清理。
    harness["created"].append(outcome.document_id)
    return outcome, payload


@pytest.mark.live_db
def test_upload_indexes_and_promotes_the_active_version(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, payload = _upload(harness, label="promote")
    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == "success"
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
    outcome, payload = _upload(harness, label="duplicate")
    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == "success"

    second = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename="duplicate-again.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(payload),
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
    upload, payload = _upload(harness, label="reindex")
    assert container.runtime.ingestion.process_job(upload.ingestion_job_id) == "success"
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
    upload, payload = _upload(harness, label="retry")
    assert container.runtime.ingestion.process_job(upload.ingestion_job_id) == "success"
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

    from backend.domain.identifiers import new_id, utc_now

    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(harness, label="recovery")
    job_id = upload.ingestion_job_id
    # 模拟一个 worker 已 claim 该 Job 后崩溃：claim 成功但长时间不再续租
    # （独立心跳线程随进程一起消失，因此租约时间戳停止推进）。
    stale_token = new_id()
    assert container.runtime.jobs.claim(job_id, owner_token=stale_token) is True
    stale = utc_now() - timedelta(hours=2)
    container.runtime.jobs.pool.execute(
        "UPDATE ingestion_jobs SET heartbeat_at = %s, updated_at = %s, created_at = %s "
        "WHERE ingestion_job_id = %s",
        (stale, stale, stale, job_id),
    )
    recovered = container.runtime.ingestion.recover_stale_jobs()
    assert recovered["stale_running"] >= 1
    job = container.runtime.jobs.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.failure_code == "stale_running_job"
    # 陈旧恢复同时撤销了执行所有权：旧 worker 无法再用它的 token 写终态。
    assert container.runtime.jobs.still_owned(job_id, owner_token=stale_token) is False
    assert container.runtime.jobs.finish(job_id, status="success", owner_token=stale_token) is False
    # 检索不再对外提供这个从未成功晋升过的 Document。
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version is None
    # 该 failed Job 可重试；原文件已在受控存储中，重试不再依赖首次上传时的内存字节，
    # 因此这里能够真正重新解析并成功（此前会因 content_unavailable 必然失败）。
    retry = container.runtime.ingestion.submit_retry(user_id=user.user_id, ingestion_job_id=job_id)
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "success"
    retried = container.runtime.jobs.get(retry.ingestion_job_id)
    assert retried is not None and retried.status == "success"
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version == retry.index_version


@pytest.mark.live_db
def test_pending_job_is_scheduled_by_maintenance_when_capacity_is_exhausted(harness):
    """容量耗尽不再让上传失败：原文件与 Job 已持久化，维护任务在有界周期内补调度。

    覆盖 CR-RUNTIME-20261009-01 的「明确接受结果 + 不留下无法恢复的 pending」：
    - worker 容量占满时 ``submit`` 返回 False，Job 仍是 ``pending``（不是 failed，也不报错）；
    - 容量恢复后 ``MaintenanceWorker.schedule_pending_jobs`` 领取并真正执行该 Job。
    """
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(harness, label="capacity")

    # 占满 worker 槽位，模拟容量耗尽。
    claimed_slots = 0
    while container.runtime.try_claim_worker_slot():
        claimed_slots += 1
    assert claimed_slots >= 1
    try:
        assert container.ingestion_worker.submit(ingestion_job_id=upload.ingestion_job_id) is False
        # 把 Job 变旧，使其进入补调度候选窗口（生产默认 15s，测试不等真实时间）。
        container.runtime.jobs.pool.execute(
            "UPDATE ingestion_jobs SET created_at = %s WHERE ingestion_job_id = %s",
            (utc_now() - timedelta(minutes=5), upload.ingestion_job_id),
        )
        # 容量仍不足：补调度跳过，但绝不把 Job 改成失败。
        assert container.maintenance.schedule_pending_jobs(limit=5) == 0
        job = container.runtime.jobs.get(upload.ingestion_job_id)
        assert job is not None and job.status == "pending"
    finally:
        for _ in range(claimed_slots):
            container.runtime.release_worker_slot()

    # 容量恢复后，同一 Job 被补调度并成功完成（claim 原子，不会重复执行）。
    assert container.maintenance.schedule_pending_jobs(limit=5) >= 1
    container.ingestion_worker.join(timeout=60)
    job = container.runtime.jobs.get(upload.ingestion_job_id)
    assert job is not None and job.status == "success"
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version == upload.index_version


@pytest.mark.live_db
def test_large_document_is_indexed_in_full_without_silent_truncation(harness):
    """ADR-036 决策 2/3/4：长文档全文进入索引，尾部内容可查，且完整性校验通过。

    这里用一个约 12 万字符、带唯一尾部标记的文档验证：
    - `document_contents.truncated == 0`，且存下的文本包含**文件结尾**的标记；
    - 该版本的 child 分块行数与向量条数一致（完整性校验通过才会 promote）；
    - 尾部标记真的出现在某个 child chunk 里（不是只存在于 contents 表）。
    """
    container = harness["container"]
    user = harness["user"]
    label = uuid.uuid4().hex[:8]
    tail_marker = f"TAIL-MARKER-{label}"
    head_marker = f"HEAD-MARKER-{label}"
    paragraphs = [f"{head_marker} section {index} " + ("term " * 40) for index in range(600)]
    paragraphs.append(f"{tail_marker} final paragraph of the document.")
    text = "\n\n".join(paragraphs)
    assert len(text) > 100_000
    payload = text.encode("utf-8")
    outcome = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename=f"large-{label}.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(payload),
    )
    harness["created"].append(outcome.document_id)

    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == "success"
    stored = container.runtime.document_contents.get(outcome.document_id)
    assert stored is not None
    assert tail_marker in stored and head_marker in stored
    contents_row = container.runtime.document_contents.pool.query_one(
        "SELECT truncated, char_count FROM document_contents WHERE document_id = %s",
        (outcome.document_id,),
    )
    assert contents_row is not None and int(contents_row[0]) == 0
    assert int(contents_row[1]) == len(stored)

    version = container.runtime.versions.get(outcome.index_version)
    assert version is not None and version.status == "active"
    child_count = container.runtime.chunks.count_for_version(
        outcome.index_version, chunk_role="child"
    )
    assert version.chunk_count == child_count and child_count > 100
    # 尾部标记必须落在某个已索引的 child chunk 中（全文覆盖，而非只覆盖前 40 万字符）。
    rows = container.runtime.chunks.pool.query(
        "SELECT COUNT(*) FROM document_chunks WHERE index_version = %s AND content LIKE %s",
        (outcome.index_version, f"%{tail_marker}%"),
    )
    assert int(rows[0][0]) >= 1
    document = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version == outcome.index_version


@pytest.mark.live_db
def test_text_over_the_explicit_budget_fails_instead_of_truncating(harness, monkeypatch):
    """超出显式预算时 Job 明确失败（extracted_text_too_large），绝不索引前缀冒充全文。"""

    from backend.infrastructure.repositories.documents import JOB_STATUS_FAILED

    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(harness, label="budget")
    # 把显式预算压到很低：文档本身很小，但预算更小，从而走「明确失败」路径。
    monkeypatch.setattr(container.settings, "max_extracted_chars", 10)
    assert container.runtime.ingestion.process_job(upload.ingestion_job_id) == JOB_STATUS_FAILED
    job = container.runtime.jobs.get(upload.ingestion_job_id)
    assert job is not None and job.status == "failed"
    assert job.failure_code == "extracted_text_too_large"
    version = container.runtime.versions.get(upload.index_version)
    assert version is not None and version.status == "failed"
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version is None
    # 受控存储中的原文件仍在：修正预算后可通过显式重试成功。
    monkeypatch.undo()
    retry = container.runtime.ingestion.submit_retry(
        user_id=user.user_id, ingestion_job_id=upload.ingestion_job_id
    )
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "success"


@pytest.mark.live_db
def test_delete_removes_the_persisted_original_file(harness):
    """删除文档时清理原文件记录与受控对象（内容寻址：仅当无其它引用）。"""
    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(harness, label="file-cleanup")
    stored = container.runtime.document_files.get(upload.document_id)
    assert stored is not None
    assert container.runtime.upload_storage.exists(stored.storage_key) is True

    outcome = container.runtime.ingestion.submit_delete(
        user_id=user.user_id, document_id=upload.document_id
    )
    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == "success"
    assert container.runtime.document_files.get(upload.document_id) is None
    assert container.runtime.upload_storage.exists(stored.storage_key) is False
    # 本用例已自行删除该文档，模块级清理不必再处理（重复删除会走 not_found 分支）。
    harness["created"].remove(upload.document_id)


@pytest.mark.live_db
def test_concurrent_claim_lets_exactly_one_worker_own_the_job(harness):
    """并发领取只有一个赢家：claim 是所有权与执行权的唯一入口（ADR-035）。"""
    import threading

    from backend.domain.identifiers import new_id

    container = harness["container"]
    upload, _payload = _upload(harness, label="concurrent-claim")
    job_id = upload.ingestion_job_id
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def claim() -> None:
        token = new_id()
        outcome = container.runtime.jobs.claim(job_id, owner_token=token)
        with lock:
            results.append((token, outcome))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    winners = [token for token, outcome in results if outcome]
    assert len(winners) == 1
    # 赢家持有所有权；输家的 token 不持有任何东西，也续不了租。
    assert container.runtime.jobs.still_owned(job_id, owner_token=winners[0]) is True
    for token, _outcome in results:
        if token != winners[0]:
            assert container.runtime.jobs.heartbeat(job_id, owner_token=token) is False

    # 用例收尾：让赢家路径真实跑完该 Job，不留下 running 行。
    container.runtime.jobs.pool.execute(
        "UPDATE ingestion_jobs SET owner_token = NULL, status = 'pending' "
        "WHERE ingestion_job_id = %s",
        (job_id,),
    )
    assert container.runtime.ingestion.process_job(job_id) == "success"


@pytest.mark.live_db
def test_promote_is_skipped_when_the_executor_lost_ownership(harness):
    """promote 提交点校验：解析/嵌入期间被陈旧恢复撤销所有权的执行者不得晋升版本。

    模拟真实竞态：嵌入过程中租约过期 → 陈旧恢复把 Job 终结为 failed 并撤销所有权
    → 旧 worker 继续走到 promote 之前的所有权校验 → 放弃写入（不 mark_validated、
    不 promote、不写终态），版本保持 failed、文档 active 指针为空。
    """
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    container = harness["container"]
    user = harness["user"]
    upload, _payload = _upload(harness, label="ownership-lost")
    job_id = upload.ingestion_job_id
    embeddings = container.runtime.embeddings
    real_embed_many = embeddings.embed_many

    def embed_many_then_revoke(texts):
        vectors = real_embed_many(texts)
        # 模拟「嵌入耗时期间租约过期、陈旧恢复接管」：把租约拨到阈值之外并运行恢复。
        stale = utc_now() - timedelta(
            seconds=container.settings.ingestion_stale_running_seconds + 60
        )
        container.runtime.jobs.pool.execute(
            "UPDATE ingestion_jobs SET heartbeat_at = %s, updated_at = %s "
            "WHERE ingestion_job_id = %s",
            (stale, stale, job_id),
        )
        assert container.runtime.ingestion.recover_stale_jobs()["stale_running"] >= 1
        return vectors

    embeddings.embed_many = embed_many_then_revoke
    try:
        assert container.runtime.ingestion.process_job(job_id) == "failed"
    finally:
        embeddings.embed_many = real_embed_many

    job = container.runtime.jobs.get(job_id)
    assert job is not None and job.status == "failed"
    assert job.failure_code == "stale_running_job"
    # 版本没有被旧执行者晋升为 active，也没有留下第二条失败码。
    version = container.runtime.versions.get(upload.index_version)
    assert version is not None and version.status == "failed"
    assert version.failure_code == "stale_running_job"
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version is None
    # 该 Job 仍有显式重试路径：重试创建新 Job/版本并成功晋升。
    retry = container.runtime.ingestion.submit_retry(user_id=user.user_id, ingestion_job_id=job_id)
    assert container.runtime.ingestion.process_job(retry.ingestion_job_id) == "success"
    document = container.runtime.documents.get(upload.document_id, user_id=user.user_id)
    assert document is not None and document.active_index_version == retry.index_version
