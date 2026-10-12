"""Document 生命周期（ADR-040 / CC-API-003）的实时集成检查。

针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。

覆盖 G19/G20 在**真实仓储与真实状态机**上的行为：

- 新文档从 ``indexing`` 开始，promote 是唯一进入 ``available`` 的路径；
- 摄取失败把**没有可用版本**的文档置为 ``failed``；
- 失败的重建索引**不降级**仍在服务旧版本的文档（受守卫的转换）；
- 重复上传报告真实 Job 状态，而不是固定 ``success``；
- 卡住/坏掉的文档可以被删除（自愈路径）。
"""

from __future__ import annotations

import uuid

import pytest

from backend.infrastructure.parser_errors import ParserError
from backend.infrastructure.parsers import ParsedDocument
from backend.infrastructure.repositories.documents import (
    DOCUMENT_STATUS_AVAILABLE,
    DOCUMENT_STATUS_FAILED,
    DOCUMENT_STATUS_INDEXING,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCESS,
)


def document_text(label: str) -> str:
    """为每个用例生成唯一内容，使内容指纹去重不会把用例耦合在一起。"""

    return (
        f"Document Lifecycle Fixture {label} {uuid.uuid4().hex}\n\n"
        "Section one explains the document lifecycle state vocabulary.\n\n"
        "Section two explains that promotion is the only path into available.\n"
    )


def blank_text(label: str) -> str:
    """只有空白字符的内容：字节数 > 0 能通过上传门控，但解析必然得到 no_extractable_text。

    长度随 label 变化，因此每个用例的字节都不同，不会因内容指纹去重而互相耦合。
    """

    return " " * (16 + len(label))


class _FailingParser:
    """总是失败的本地 parser：用来真实地走一遍摄取失败路径。"""

    provider_name = "lightweight"

    def provider_for(self, *, filename: str, content_type: str) -> str:
        return self.provider_name

    def parse(self, *, filename: str, content_type: str, data: bytes) -> ParsedDocument:
        raise ParserError("no_extractable_text")


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
    created: list[str] = []
    yield {"container": container, "user": user, "settings": settings, "created": created}
    for document_id in created:
        try:
            outcome = container.runtime.ingestion.submit_delete(
                user_id=user.user_id, document_id=document_id
            )
            container.runtime.ingestion.process_job(outcome.ingestion_job_id)
        except Exception as error:  # noqa: BLE001 - 清理失败不得影响测试结论
            print(f"lifecycle_fixture_cleanup_failed: {document_id[:8]} {type(error).__name__}")
    container.close()


def _upload(harness, *, label: str, text: str | None = None):
    container = harness["container"]
    user = harness["user"]
    payload = (text or document_text(label)).encode("utf-8")
    outcome = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename=f"{label}-{uuid.uuid4().hex[:8]}.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(payload),
    )
    assert outcome.duplicate is False, "fixture content must be unique"
    harness["created"].append(outcome.document_id)
    return outcome, payload


@pytest.mark.live_db
def test_new_document_is_indexing_until_promotion_makes_it_available(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, _payload = _upload(harness, label="promote-status")

    before = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert before is not None
    # G19 根因：上传即 available。现在必须先 indexing。
    assert before.status == DOCUMENT_STATUS_INDEXING
    assert before.active_index_version is None
    # 尚未晋升的文档不在授权集合里，因此不能被选为作用域、也不可检索。
    assert outcome.document_id not in container.runs._authorized_document_ids(user.user_id)

    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == JOB_STATUS_SUCCESS
    after = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert after is not None
    assert after.status == DOCUMENT_STATUS_AVAILABLE
    assert after.active_index_version == outcome.index_version


@pytest.mark.live_db
def test_failed_ingestion_marks_a_document_without_a_version_as_failed(harness):
    container = harness["container"]
    user = harness["user"]
    # 只有空白字符：字节数 > 0 能通过上传门控，但解析必然得到 no_extractable_text。
    outcome, _payload = _upload(harness, label="fail", text=blank_text("fail"))

    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == JOB_STATUS_FAILED
    job = container.runtime.jobs.get(outcome.ingestion_job_id)
    assert job is not None and job.status == JOB_STATUS_FAILED
    document = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert document is not None
    assert document.status == DOCUMENT_STATUS_FAILED
    assert document.active_index_version is None


@pytest.mark.live_db
def test_failed_reindex_does_not_downgrade_a_document_that_still_serves_a_version(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, _payload = _upload(harness, label="guard")
    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == JOB_STATUS_SUCCESS

    reindex = container.runtime.ingestion.submit_reindex(
        user_id=user.user_id, document_id=outcome.document_id
    )
    # 用一个总是失败的 parser 真实地走一遍失败路径（重建索引复用受控存储中的原文件）。
    original_parser = container.runtime.ingestion.parser
    container.runtime.ingestion.parser = _FailingParser()
    try:
        assert (
            container.runtime.ingestion.process_job(reindex.ingestion_job_id) == JOB_STATUS_FAILED
        )
    finally:
        container.runtime.ingestion.parser = original_parser

    document = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert document is not None
    # 受守卫的转换：文档仍有可用 active 版本，因此不能被降级为 failed。
    assert document.status == DOCUMENT_STATUS_AVAILABLE
    assert document.active_index_version == outcome.index_version
    # 失败的版本自身必须是 failed，而不是 active。
    failed_version = container.runtime.versions.get(reindex.index_version)
    assert failed_version is not None and failed_version.status == "failed"


@pytest.mark.live_db
def test_duplicate_upload_reports_the_real_job_status(harness):
    container = harness["container"]
    user = harness["user"]

    # 1) 失败态：重复上传必须报 failed，而不是硬编码的 success（G20）。
    broken, broken_payload = _upload(harness, label="dup-fail", text=blank_text("dup-fail"))
    assert container.runtime.ingestion.process_job(broken.ingestion_job_id) == JOB_STATUS_FAILED
    again = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename="dup-fail-again.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(broken_payload),
    )
    assert again.duplicate is True
    assert again.document_id == broken.document_id
    assert again.status == JOB_STATUS_FAILED
    assert again.ingestion_job_id == broken.ingestion_job_id

    # 2) 成功态：晋升之后重复上传才报 success。
    healthy, healthy_payload = _upload(harness, label="dup-ok")
    assert container.runtime.ingestion.process_job(healthy.ingestion_job_id) == JOB_STATUS_SUCCESS
    repeat = container.runtime.ingestion.submit_upload(
        user_id=user.user_id,
        filename="dup-ok-again.md",
        content_type="text/markdown",
        stored=container.runtime.upload_storage.store_bytes(healthy_payload),
    )
    assert repeat.duplicate is True
    assert repeat.status == JOB_STATUS_SUCCESS
    assert repeat.document_id == healthy.document_id


@pytest.mark.live_db
def test_a_stuck_document_can_be_deleted_through_the_api(harness):
    container = harness["container"]
    user = harness["user"]
    outcome, _payload = _upload(harness, label="delete-stuck", text=blank_text("delete-stuck"))
    assert container.runtime.ingestion.process_job(outcome.ingestion_job_id) == JOB_STATUS_FAILED
    stuck = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert stuck is not None and stuck.status == DOCUMENT_STATUS_FAILED

    # 坏掉的文档必须能被删除（否则用户没有任何自愈路径）。
    delete = container.runtime.ingestion.submit_delete(
        user_id=user.user_id, document_id=outcome.document_id
    )
    assert container.runtime.ingestion.process_job(delete.ingestion_job_id) == JOB_STATUS_SUCCESS
    after = container.runtime.documents.get(outcome.document_id, user_id=user.user_id)
    assert after is None or after.status == "deleted"
