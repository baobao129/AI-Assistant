"""SSE 回放保留语义的实时集成检查（API-005）。

Run 事件在有限窗口内持久化以支持回放。当客户端携带一个已被裁剪的 ``Last-Event-ID``
重连时，服务端必须返回 ``409 conflict``（replay unavailable），而不是静默回放一段
不完整的事件流；仍位于保留窗口内的回放位置必须继续正常回放。

本测试调小 ``sse_replay_retention_events``，让保留边界可以在单个用例内触达，然后通过
真实的发布器与仓库把 Run 的事件数推过该边界。测试针对已配置的应用数据库运行；
未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture(scope="module")
def harness(live_settings):
    """构建带登录会话的真实应用容器，供本模块的用例共享。"""

    from fastapi.testclient import TestClient

    from backend.api.dependencies import build_container
    from backend.api.main import create_app

    # 调小但合法的保留窗口，使裁剪边界能在单个用例内触达。
    settings = live_settings.model_copy(update={"sse_replay_retention_events": 10})
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
            "/api/v1/conversations", json={"title": "sse replay integration"}
        ).json()["conversation_id"]
        yield {"client": client, "container": container, "conversation_id": conversation_id}
    container.close()


def _await_status(client, run_id: str, statuses: set[str], *, deadline: float = 60.0) -> str:
    """轮询 Run 状态，直到进入目标状态集合或超过截止时间。"""

    started = time.monotonic()
    status = "unknown"
    while time.monotonic() - started < deadline:
        status = client.get(f"/api/v1/runs/{run_id}").json()["status"]
        if status in statuses:
            return status
        time.sleep(0.5)
    return status


@pytest.mark.live_db
def test_expired_last_event_id_replay_returns_409(harness):
    client = harness["client"]
    container = harness["container"]
    conversation_id = harness["conversation_id"]

    # 先跑完一个普通 Run，作为事件流与回放位置的载体。
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"user_input": "hello there"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert _await_status(client, run_id, {"completed", "failed"}) == "completed"

    run = container.runtime.runs.get(run_id)
    assert run is not None
    history = container.events.history(run_id=run_id, after_event_id=None)
    assert history, "该 Run 没有产生任何事件"
    stale_event_id = history[0].event_id

    # 存储层每发布 50 个事件执行一次裁剪，因此一次性补发 50 个事件必定触发至少一次裁剪。
    for _ in range(50):
        container.events.publish(
            run_id=run_id,
            thread_id=run.thread_id,
            event_type="token",
            payload={"delta": "x"},
        )

    retained = container.events.history(run_id=run_id, after_event_id=None)
    assert stale_event_id not in {event.event_id for event in retained}

    # 过期回放必须返回 409 conflict（replay unavailable）。
    expired = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": stale_event_id})
    assert expired.status_code == 409, expired.text
    assert expired.json()["error"]["code"] == "conflict"

    # 仍在保留窗口内的回放位置必须继续正常回放。
    within = client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": retained[-1].event_id}
    )
    assert within.status_code == 200, within.text
