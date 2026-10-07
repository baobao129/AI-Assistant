"""Critical E2E/Smoke checks.

The dependency-health check runs against whatever ``SMOKE_BASE_URL`` points at. The
authenticated Demo flow additionally requires ``SMOKE_USERNAME`` and ``SMOKE_PASSWORD``
and exercises login, Conversation/Run creation, SSE event ordering, replay, and the
Document upload path through the real HTTP surface.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest


def _base_url() -> str:
    url = os.getenv("SMOKE_BASE_URL")
    if not url:
        pytest.skip("Set SMOKE_BASE_URL to the running frontend URL for live acceptance.")
    return url.rstrip("/")


@pytest.mark.smoke
def test_live_runtime():
    with httpx.Client(base_url=_base_url(), timeout=15) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Enterprise Multi-Source AI Agent" in page.text
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["services"] == {
            "backend": "healthy",
            "mysql": "healthy",
            "milvus": "healthy",
        }


def _read_events(client: httpx.Client, run_id: str, headers: dict[str, str]) -> list[dict]:
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

        # Replay from the first event returns the retained stream, not a new Run.
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

        # Documents are listed from the application registry, never from Milvus.
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
