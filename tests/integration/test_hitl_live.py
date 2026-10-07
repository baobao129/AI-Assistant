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
