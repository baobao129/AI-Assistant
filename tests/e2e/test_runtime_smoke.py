"""关键 E2E/Smoke 检查。

依赖健康检查针对 ``SMOKE_BASE_URL`` 指向的服务运行。带认证的 Demo 流程还额外需要
``SMOKE_USERNAME`` 与 ``SMOKE_PASSWORD``，并通过真实 HTTP 接口走一遍登录、Conversation/Run
创建、SSE 事件顺序、回放以及 Document 上传路径。
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest


def _base_url() -> str:
    # 未配置目标地址时跳过，保证默认套件不因缺少实时运行时而失败。
    url = os.getenv("SMOKE_BASE_URL")
    if not url:
        pytest.skip("Set SMOKE_BASE_URL to the running frontend URL for live acceptance.")
    return url.rstrip("/")


@pytest.mark.smoke
def test_live_runtime():
    with httpx.Client(base_url=_base_url(), timeout=15) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<title>企业多源智能助手</title>" in page.text
        assert 'lang="zh-CN"' in page.text
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["services"] == {
            "backend": "healthy",
            "mysql": "healthy",
            "milvus": "healthy",
        }


def _read_events(client: httpx.Client, run_id: str, headers: dict[str, str]) -> list[dict]:
    # 有界读取 SSE：遇到终态 final/error 或超过 60 秒截止时间即停止。
    events: list[dict] = []
    deadline = time.monotonic() + 60
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event["event_type"] in {"final", "error"}:
                break
    return events


@pytest.mark.smoke
def test_live_authenticated_demo_flow():
    username = os.getenv("SMOKE_USERNAME")
    password = os.getenv("SMOKE_PASSWORD")
    if not username or not password:
        pytest.skip("Set SMOKE_USERNAME and SMOKE_PASSWORD for the authenticated Demo flow.")

    with httpx.Client(base_url=_base_url(), timeout=30) as client:
        anonymous = client.get("/api/v1/conversations")
        assert anonymous.status_code == 401

        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['session']['token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).json()["username"] == username

        conversation_id = client.post(
            "/api/v1/conversations", json={"title": "smoke"}, headers=headers
        ).json()["conversation_id"]

        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "hello there"},
            headers=headers,
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        assert accepted.json()["stream_url"] == f"/api/v1/runs/{run_id}/events"

        events = _read_events(client, run_id, headers)
        sequences = [event["sequence"] for event in events]
        assert sequences == list(range(1, len(sequences) + 1)), (
            "sequence must start at 1 and increase"
        )
        assert events[0]["event_type"] == "agent_start"
        assert events[-1]["event_type"] in {"final", "error"}
        assert all(event["run_id"] == run_id for event in events)
        assert len({event["event_id"] for event in events}) == len(events)

        # 从首个事件开始回放，应返回保留的事件流，而不是一个新的 Run。
        last_event_id = events[0]["event_id"]
        with client.stream(
            "GET",
            f"/api/v1/runs/{run_id}/events",
            headers={**headers, "Last-Event-ID": last_event_id},
        ) as response:
            assert response.status_code == 200
            replayed = [
                json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")
            ]
        assert [event["sequence"] for event in replayed][:1] == [2]
        assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "completed"

        # Document 列表来自应用注册表，绝不直接查 Milvus。
        listed = client.get("/api/v1/documents", headers=headers)
        assert listed.status_code == 200
        assert "items" in listed.json()

        preview = client.get(
            "/api/v1/data-sources/demo_sales/tables/sales_orders/preview?limit=5", headers=headers
        )
        assert preview.status_code == 200
        assert preview.json()["row_count"] <= 5
        excessive = client.get(
            "/api/v1/data-sources/demo_sales/tables/sales_orders/preview?limit=5000",
            headers=headers,
        )
        assert excessive.status_code == 422
