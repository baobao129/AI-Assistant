"""文档作用域（ADR-039）的实时集成检查。

覆盖：带作用域的请求真的走 Knowledge（业务数据关键词不再抢走它）、证据只来自选中文档、
他人/不可用文档返回统一的 409 且不泄漏存在性、带作用域的寒暄仍走 General、
以及作用域跨 Checkpoint 的 Resume 保留。

未导出实时凭据时整个模块被跳过（见 ``tests/conftest.py``）。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.domain.state import initial_state
from backend.graph.main import build_main_graph
from backend.tools.contracts import ToolExecutionContext
from backend.tools.data_tools import KnowledgeSearchInput, KnowledgeSearchTool


def _fixture_text(label: str) -> str:
    return f"Document Scope Fixture {label} {uuid.uuid4().hex}\n\n" + (
        "policy clause about approval limits and escalation " * 40
    )


def _read_stream(client, run_id: str, headers: dict[str, str], *, deadline: float = 90.0):
    events: list[dict] = []
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
        if response.status_code != 200:
            return events
        for line in response.iter_lines():
            if time.monotonic() - started > deadline:
                break
            if not line or not line.startswith("data: "):
                continue
            events.append(json.loads(line[6:]))
            if events[-1]["event_type"] in {"final", "error"}:
                break
    return events


def _await_job(client, job_id: str, headers: dict[str, str], *, deadline: float = 120.0) -> str:
    started = time.monotonic()
    while time.monotonic() - started < deadline:
        response = client.get(f"/api/v1/ingestion-jobs/{job_id}", headers=headers)
        if response.status_code == 200:
            status = response.json()["status"]
            if status in {"success", "failed"}:
                return str(status)
        time.sleep(1.0)
    return "timeout"


@pytest.fixture(scope="module")
def harness(live_settings):
    from fastapi.testclient import TestClient

    from backend.api.dependencies import build_container
    from backend.api.main import create_app

    settings = live_settings
    container = build_container(settings)
    user = container.runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    if user is None:
        user = container.runtime.auth.create_user(
            username=settings.auth_bootstrap_username,
            password=settings.auth_bootstrap_password.get_secret_value(),
            display_name=settings.auth_bootstrap_display_name,
        )
    app = create_app(settings, container=container)
    created: list[str] = []
    # ``app.state.container`` 由 lifespan 装配，因此 TestClient 必须当作上下文管理器
    # 使用；清理也要在 with 内部完成（退出时 lifespan 会关闭 Container）。
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": settings.auth_bootstrap_username,
                "password": settings.auth_bootstrap_password.get_secret_value(),
            },
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['session']['token']}"}

        def upload(label: str) -> str:
            text = _fixture_text(label)
            response = client.post(
                "/api/v1/documents",
                headers=headers,
                files={"file": (f"scope-{label}-{uuid.uuid4().hex[:8]}.md", text, "text/markdown")},
            )
            assert response.status_code == 202, response.text
            payload = response.json()
            document_id = payload["document_id"]
            created.append(document_id)
            if payload.get("ingestion_job_id"):
                assert _await_job(client, payload["ingestion_job_id"], headers) == "success"
            return document_id

        try:
            yield {
                "client": client,
                "headers": headers,
                "container": container,
                "upload": upload,
                "settings": settings,
                "user": user,
                "created": created,
            }
        finally:
            for document_id in created:
                try:
                    outcome = container.runtime.ingestion.submit_delete(
                        user_id=user.user_id, document_id=document_id
                    )
                    container.runtime.ingestion.process_job(outcome.ingestion_job_id)
                except Exception as error:  # noqa: BLE001 - 清理失败不影响测试结论
                    print(f"scope_fixture_cleanup_failed: {document_id[:8]} {type(error).__name__}")


def _start_run(client, headers: dict[str, str], *, user_input: str, document_ids=None) -> dict:
    conversation = client.post(
        "/api/v1/conversations", json={"title": "Document scope"}, headers=headers
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["conversation_id"]
    body: dict[str, object] = {"user_input": user_input}
    if document_ids is not None:
        body["document_ids"] = document_ids
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/runs", json=body, headers=headers
    )
    return {
        "status_code": response.status_code,
        "body": response.json(),
        "conversation_id": conversation_id,
    }


@pytest.mark.live_db
def test_a_scoped_business_keyword_question_is_answered_from_knowledge(harness):
    """验收核心：选中文档后，「金额」这类结构化数据关键词不能把请求抢到 SQL 路径。"""

    client, headers = harness["client"], harness["headers"]
    document_id = harness["upload"]("scoped-knowledge")

    started = _start_run(
        client,
        headers,
        user_input="how much is the sales amount and the count of approvals in this document",
        document_ids=[document_id],
    )
    assert started["status_code"] == 202, started["body"]
    run_id = started["body"]["run_id"]
    events = _read_stream(client, run_id, headers)
    assert events, "the run must produce a stream"

    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["routing_outcome"] == "routed"
    assert run["intent"] == "knowledge", run
    # 关键：绝不能落到 structured_data。
    assert "tool_call" not in {event["event_type"] for event in events} or all(
        event.get("payload", {}).get("tool_id") != "database_query" for event in events
    )


@pytest.mark.live_db
def test_a_scoped_greeting_still_goes_to_general(harness):
    client, headers = harness["client"], harness["headers"]
    document_id = harness["upload"]("scoped-greeting")

    started = _start_run(client, headers, user_input="hello there", document_ids=[document_id])
    assert started["status_code"] == 202, started["body"]
    run_id = started["body"]["run_id"]
    _read_stream(client, run_id, headers)

    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["intent"] == "general", run


@pytest.mark.live_db
def test_retrieval_evidence_comes_only_from_the_selected_document(harness):
    """在真实索引上直接验证检索边界：结果里的 document_id 只能来自作用域。"""

    container = harness["container"]
    selected = harness["upload"]("scope-only")
    other = harness["upload"]("scope-other")

    authorized = tuple(
        document.document_id
        for document in container.runtime.documents.list(
            user_id=harness["user"].user_id, limit=100, cursor=None
        )[0]
    )
    assert selected in authorized and other in authorized

    tool = KnowledgeSearchTool(
        settings=harness["settings"],
        embeddings=container.runtime.embeddings,
        vectors=container.runtime.vectors,
        versions=container.runtime.versions,
        documents=container.runtime.documents,
        chunks=container.runtime.chunks,
        registry=container.runtime.registry,
    )
    context = ToolExecutionContext(
        user_id=harness["user"].user_id, authorized_document_ids=authorized
    )

    scoped = tool.execute(
        "call-scoped",
        KnowledgeSearchInput(
            query="policy clause about approval limits", top_k=5, document_ids=[selected]
        ),
        context,
    )
    assert scoped.status == "success", scoped.error
    returned = {item["document_id"] for item in scoped.data["results"]}
    assert returned <= {selected}, returned
    for item in scoped.data["results"]:
        assert item["citation"]["source_id"] == selected

    # 无作用域时仍可跨文档召回：证明作用域是「收窄」而不是「换了一套检索」。
    unscoped = tool.execute(
        "call-unscoped",
        KnowledgeSearchInput(query="policy clause about approval limits", top_k=20),
        context,
    )
    assert unscoped.status == "success", unscoped.error
    assert unscoped.data["results"], "the unscoped retrieval must still find the fixtures"


@pytest.mark.live_db
def test_a_document_owned_by_another_user_is_rejected_without_leaking(harness):
    client, headers = harness["client"], harness["headers"]
    container = harness["container"]

    other_user = container.runtime.auth.get_user_by_username("scope-other-user")
    if other_user is None:
        other_user = container.runtime.auth.create_user(
            username="scope-other-user",
            password="scope-other-password",
            display_name="Scope Other",
        )
    foreign = container.runtime.documents.create(
        user_id=other_user.user_id,
        title="foreign",
        source_name="foreign.md",
        content_type="text/markdown",
        content_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
        byte_size=10,
    )
    try:
        started = _start_run(
            client, headers, user_input="policy", document_ids=[foreign.document_id]
        )
        assert started["status_code"] == 409, started["body"]
        error = started["body"]["error"]
        assert error["code"] == "document_scope_unavailable"
        # 不泄漏存在性：文案里不能出现该 ID，也不能说明原因。
        assert foreign.document_id not in error["message"]
        assert "not found" not in error["message"].casefold()
    finally:
        # 夹具必须自清理：否则每次运行都会在演示库里留下一个他人文档，
        # 反复运行会把共享语料撑大（并让「选第一项」之类的验收脚本变得不稳定）。
        container.runtime.documents.mark_deleted(foreign.document_id, user_id=other_user.user_id)


@pytest.mark.live_db
def test_an_unknown_document_id_is_rejected_with_the_same_code(harness):
    client, headers = harness["client"], harness["headers"]
    started = _start_run(client, headers, user_input="policy", document_ids=["f" * 32])
    assert started["status_code"] == 409, started["body"]
    assert started["body"]["error"]["code"] == "document_scope_unavailable"


@pytest.mark.live_db
def test_a_malformed_scope_is_rejected_by_format_validation(harness):
    client, headers = harness["client"], harness["headers"]
    for bad in (["../../etc/passwd"], ["https://example.com/a.pdf"], ["report.pdf"]):
        started = _start_run(client, headers, user_input="policy", document_ids=bad)
        assert started["status_code"] == 422, started["body"]


def test_the_scope_survives_the_checkpoint_for_resume():
    """作用域必须由 Checkpoint 承载：Resume 只传身份增量，绝不能用空值覆盖它。"""

    checkpointer = InMemorySaver()
    graph = build_main_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "scope-thread"}}
    state = initial_state(
        conversation_id="c1",
        thread_id="scope-thread",
        run_id="r1",
        user_input="hello there",
        document_scope=["doc-1", "doc-2"],
    )
    graph.invoke(state, config)

    snapshot = graph.get_state(config)
    assert snapshot.values["document_scope"] == ["doc-1", "doc-2"]

    # 模拟 Resume：只传身份 + approval 绑定这一有界增量（与 RunService.resume 一致），
    # 其余由 Checkpoint 恢复。作用域必须仍然是原值，而不是被清空。
    graph.invoke(
        {
            "conversation_id": "c1",
            "thread_id": "scope-thread",
            "run_id": "r2",
            "user_input": "hello there",
            "approval_request_id": None,
            "interrupt_status": "none",
            "query_result_id": None,
        },
        config,
    )
    resumed = graph.get_state(config)
    assert resumed.values["document_scope"] == ["doc-1", "doc-2"]
