"""Query Result 快照溯源的实时集成检查。

一次成功的 ``database_query`` 会写入一份不可变的 Query Result 快照。State 契约要求
``query_result_id`` 引用能够体现其「产生执行（producer execution）、授权范围
（authorization scope）与保留元数据（retention metadata）」。

本测试通过真实链路驱动一次 Structured Data Run：
API -> Graph -> Tool Executor -> ``database_query`` Tool -> 只读账号 -> ``QueryResultRepository``，
并断言持久化的快照可以追溯到产生它的那一个具体 Run。

测试针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。
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
            "/api/v1/conversations", json={"title": "query snapshot integration"}
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
def test_snapshot_is_traceable_to_the_producing_run(harness):
    client = harness["client"]
    container = harness["container"]
    conversation_id = harness["conversation_id"]

    # 通过真实 API 发起一次结构化数据 Run；离线 profile 下由确定性脚本模型生成查询。
    accepted = client.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"user_input": "show sales amount"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert _await_status(client, run_id, {"completed", "failed"}) == "completed"

    run = container.runtime.runs.get(run_id)
    assert run is not None and run.query_result_id, "该 Run 未记录 query_result_id"

    snapshot = container.runtime.query_results.get(run.query_result_id)
    assert snapshot is not None
    # 产生执行：快照记录了产生它的那一个具体 Run。
    assert snapshot.run_id == run_id
    # 授权范围：快照归属于产生它的用户。
    assert snapshot.user_id == run.user_id
    assert snapshot.conversation_id == conversation_id
    # 保留元数据：快照带有创建时间与过期时间。
    assert snapshot.expires_at > snapshot.created_at
