"""HITL 审批状态机的实时集成检查。

这些用例针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。
覆盖等待中的 Run、后继执行创建、重复审批决定、拒绝，以及不产生副作用的过期。
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(scope="module")
def harness(live_settings):
    # 模块级共享一个已登录的真实容器，并在其中创建一个会话用于发起 Run。
    from fastapi.testclient import TestClient

    from backend.api.dependencies import build_container
    from backend.api.main import create_app

    settings = live_settings
    container = build_container(settings)
    with TestClient(create_app(settings, container=container)) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": settings.auth_bootstrap_username,
                "password": settings.auth_bootstrap_password.get_secret_value(),
            },
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['session']['token']}"}
        client.headers.update(headers)
        conversation_id = client.post(
            "/api/v1/conversations", json={"title": "hitl integration"}
        ).json()["conversation_id"]
        yield {"client": client, "container": container, "conversation_id": conversation_id}
    container.close()


def _await_status(client, run_id: str, statuses: set[str], *, deadline: float = 60.0) -> str:
    # 有界轮询：持续查询 Run 状态直到命中目标集合或超时，避免死等。
    started = time.monotonic()
    status = "unknown"
    while time.monotonic() - started < deadline:
        status = client.get(f"/api/v1/runs/{run_id}").json()["status"]
        if status in statuses:
            return status
        time.sleep(0.5)
    return status


def _start_export(client, conversation_id: str) -> tuple[str, str]:
    # data_export 属高风险操作，触发 HITL：Run 应停在 awaiting_approval。
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"user_input": "export sales amount data to csv"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    status = _await_status(client, run_id, {"awaiting_approval", "failed", "completed"})
    assert status == "awaiting_approval", status
    events = client.get(f"/api/v1/runs/{run_id}").json()
    assert events["status"] == "awaiting_approval"
    approval_request_id = _approval_request_id(container_run_events(client, run_id))
    return run_id, approval_request_id


def container_run_events(client, run_id: str) -> list[dict]:
    # 读取 SSE 直到 approval_required：该事件之后等待中的 Run 会主动关闭连接。
    import json

    events: list[dict] = []
    with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["event_type"] == "approval_required":
                    break
    return events


def _read_all_events(client, run_id: str, *, deadline: float = 60.0) -> list[dict]:
    """读取一个已终态 Run 的完整（保留窗口内）事件历史。"""

    import json

    events: list[dict] = []
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
        for line in response.iter_lines():
            if time.monotonic() - started > deadline:
                break
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["event_type"] in {"final", "error"}:
                    break
    return events


def _approval_request_id(events: list[dict]) -> str:
    approval = next(event for event in events if event["event_type"] == "approval_required")
    return approval["payload"]["approval_request_id"]


@pytest.mark.live_db
def test_waiting_run_approval_creates_successor_and_is_idempotent(harness):
    # Approve 按 approval_request_id 幂等：重复审批返回同一 successor，
    # 且不能再对该请求执行 Reject（返回 409）。
    client = harness["client"]
    conversation_id = harness["conversation_id"]
    run_id, approval_request_id = _start_export(client, conversation_id)
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    correlation = {
        "conversation_id": conversation_id,
        "thread_id": detail["thread_id"],
        "run_id": run_id,
    }

    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    successor = decision.json()["successor_run_id"]
    assert successor
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "handoff_complete"
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"

    repeat = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert repeat.status_code == 202
    assert repeat.json()["successor_run_id"] == successor

    conflicting = client.post(f"/api/v1/approvals/{approval_request_id}/reject", json=correlation)
    assert conflicting.status_code == 409


@pytest.mark.live_db
def test_rejection_is_terminal_and_creates_no_successor(harness):
    # Reject 是终态且不创建后继执行；重复 Reject 幂等返回 resume_scheduled。
    client = harness["client"]
    conversation_id = harness["conversation_id"]
    run_id, approval_request_id = _start_export(client, conversation_id)
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    correlation = {
        "conversation_id": conversation_id,
        "thread_id": detail["thread_id"],
        "run_id": run_id,
    }

    decision = client.post(f"/api/v1/approvals/{approval_request_id}/reject", json=correlation)
    assert decision.status_code == 202, decision.text
    assert decision.json()["successor_run_id"] is None
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "rejected"
    repeat = client.post(f"/api/v1/approvals/{approval_request_id}/reject", json=correlation)
    assert repeat.status_code == 202
    assert repeat.json()["status"] == "resume_scheduled"


@pytest.mark.live_db
def test_expiry_makes_the_run_expired_without_a_successor(harness):
    # 过期后 Run 变为 expired，且不能再用原审批执行（409），不会产生副作用。
    client = harness["client"]
    container = harness["container"]
    conversation_id = harness["conversation_id"]
    run_id, approval_request_id = _start_export(client, conversation_id)

    # 通过 worker 使用的同一条有界维护路径把审批置为过期，而不是直接改状态字段。
    from backend.domain.identifiers import utc_now

    container.runtime.approvals.pool.execute(
        "UPDATE approvals SET expires_at = %s WHERE approval_request_id = %s",
        (utc_now(), approval_request_id),
    )
    expired = container.approvals.expire_due(limit=10)
    assert approval_request_id in expired
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "expired"
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    decision = client.post(
        f"/api/v1/approvals/{approval_request_id}/approve",
        json={
            "conversation_id": conversation_id,
            "thread_id": detail["thread_id"],
            "run_id": run_id,
        },
    )
    assert decision.status_code == 409
    approval = container.runtime.approvals.get(approval_request_id)
    assert approval is not None and approval.status == "expired"
    assert approval.artifact_id is None


@pytest.mark.live_db
def test_approval_required_payload_carries_contract_fields_and_strict_sequence(harness):
    # API-005：approval_required 必须携带 approval_request_id / conversation_id / thread_id /
    # run_id / 生命周期 / 过期时间与安全的用户可见摘要；sequence 从 1 起严格递增。
    client = harness["client"]
    conversation_id = harness["conversation_id"]
    run_id, _ = _start_export(client, conversation_id)

    events = container_run_events(client, run_id)
    # 服务端在补发历史后丢弃订阅通道中已补发过的 sequence，因此原始流即严格递增且不重复。
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len({event["event_id"] for event in events}) == len(events)

    approval = next(event for event in events if event["event_type"] == "approval_required")
    payload = approval["payload"]
    assert set(payload) >= {
        "approval_request_id",
        "conversation_id",
        "thread_id",
        "run_id",
        "approval_lifecycle",
        "expires_at",
        "summary",
    }
    assert payload["conversation_id"] == conversation_id
    assert payload["run_id"] == run_id
    assert payload["thread_id"] == approval["thread_id"]
    assert payload["approval_lifecycle"] == "pending"
    assert payload["expires_at"]
    # 摘要只暴露用户可见的导出元数据，绝不包含完整 Graph State 或隐藏推理。
    summary = payload["summary"]
    assert set(summary) <= {"row_count", "columns", "format", "reason"}
    assert "messages" not in payload and "tool_results" not in payload


@pytest.mark.live_db
def test_repeat_approve_does_not_dispatch_or_execute_again(harness):
    # MAJOR 3/4：重复 approve 不再 dispatch；successor 只执行一次 export，
    # 不重复 database_query，使用审批绑定的同一个 snapshot，且只产生一个 final。
    client = harness["client"]
    container = harness["container"]
    conversation_id = harness["conversation_id"]
    run_id, approval_request_id = _start_export(client, conversation_id)
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    correlation = {
        "conversation_id": conversation_id,
        "thread_id": detail["thread_id"],
        "run_id": run_id,
    }

    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    successor = decision.json()["successor_run_id"]
    assert successor
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"

    successor_events = _read_all_events(client, successor)
    tool_call_ids = [
        event["payload"].get("tool_id")
        for event in successor_events
        if event["event_type"] == "tool_call"
    ]
    assert tool_call_ids.count("data_export") == 1
    assert "database_query" not in tool_call_ids
    assert [event["event_type"] for event in successor_events].count("final") == 1

    # successor 使用审批绑定的不可变 snapshot，而不是重新查询出来的新快照。
    approval = container.runtime.approvals.get(approval_request_id)
    bound_query_result_id = approval.binding["query_result_id"]
    original_record = container.runtime.runs.get(run_id)
    successor_record = container.runtime.runs.get(successor)
    assert original_record.query_result_id == bound_query_result_id
    assert successor_record.query_result_id == bound_query_result_id
    artifact = container.runtime.artifacts.get_by_approval(approval_request_id)
    assert artifact is not None

    # 重复 approve：返回同一 successor，且不产生第二次执行/第二个 final/第二个 artifact。
    repeat = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert repeat.status_code == 202
    assert repeat.json()["successor_run_id"] == successor

    after_repeat = _read_all_events(client, successor)
    assert [event["event_type"] for event in after_repeat].count("final") == 1
    assert [event["event_id"] for event in after_repeat] == [
        event["event_id"] for event in successor_events
    ]
    again = container.runtime.artifacts.get_by_approval(approval_request_id)
    assert again is not None and again.artifact_id == artifact.artifact_id
