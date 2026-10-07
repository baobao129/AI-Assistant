"""Focused live diagnostics for the Compose-backed runtime.

These are development aids for failure investigation; ``scripts/verify_live.py`` holds the
acceptance evidence. Run them with ``scripts/dev-env.ps1`` loaded.

Usage:

    uv run python -m scripts.diagnostics stream    # one Run's SSE stream
    uv run python -m scripts.diagnostics repeat    # several Runs plus the export flow
    uv run python -m scripts.diagnostics service   # Run/Approval services without HTTP
"""

from __future__ import annotations

import json
import logging
import sys
import time

from scripts.live_defaults import live_credentials_missing
from scripts.verify_live import live_settings


def _client():
    from fastapi.testclient import TestClient

    from backend.api.dependencies import build_container
    from backend.api.main import create_app

    settings = live_settings()
    missing = live_credentials_missing()
    if missing:
        raise SystemExit(
            f"Missing live credentials: {', '.join(missing)}; run scripts/dev-env.ps1."
        )
    container = build_container(settings)
    return TestClient(create_app(settings, container=container)), container, settings


def _headers(client, settings) -> dict[str, str]:
    login = client.post(
        "/api/v1/auth/login",
        json={
            "username": settings.auth_bootstrap_username,
            "password": settings.auth_bootstrap_password.get_secret_value(),
        },
    )
    return {"Authorization": f"Bearer {login.json()['session']['token']}"}


def _drain(client, run_id: str, headers: dict[str, str], *, deadline: float = 30.0) -> list[dict]:
    events: list[dict] = []
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
        for line in response.iter_lines():
            if time.monotonic() - started > deadline:
                break
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                print(f"  {event['sequence']} {event['event_type']}", flush=True)
                if event["event_type"] in {"final", "error"}:
                    break
    return events


def command_stream() -> int:
    client, container, settings = _client()
    with client:
        headers = _headers(client, settings)
        conversation_id = client.post(
            "/api/v1/conversations", json={"title": "diagnostics"}, headers=headers
        ).json()["conversation_id"]
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "show sales amount by region"},
            headers=headers,
        )
        _drain(client, accepted.json()["run_id"], headers)
    container.close()
    return 0


def command_repeat() -> int:
    client, container, settings = _client()
    with client:
        headers = _headers(client, settings)
        conversation_id = client.post(
            "/api/v1/conversations", json={"title": "diagnostics"}, headers=headers
        ).json()["conversation_id"]
        for index in range(3):
            print(f"--- structured run {index}", flush=True)
            accepted = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={"user_input": "show sales amount by region"},
                headers=headers,
            )
            _drain(client, accepted.json()["run_id"], headers)
        print("--- export flow", flush=True)
        export = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "export sales amount data to csv"},
            headers=headers,
        )
        run_id = export.json()["run_id"]
        deadline = time.monotonic() + 60
        status = "unknown"
        while time.monotonic() < deadline:
            status = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"]
            if status in {"awaiting_approval", "completed", "failed"}:
                break
            time.sleep(0.5)
        print("  export status:", status, flush=True)
        _drain(client, run_id, headers)
    container.close()
    return 0


def command_service() -> int:
    from backend.api.dependencies import build_container

    settings = live_settings()
    missing = live_credentials_missing()
    if missing:
        raise SystemExit(
            f"Missing live credentials: {', '.join(missing)}; run scripts/dev-env.ps1."
        )
    container = build_container(settings)
    runtime = container.runtime
    user = runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    conversation = runtime.conversations.create(user_id=user.user_id, title="service diagnostics")
    handle = container.runs.start_run(
        user_id=user.user_id,
        conversation_id=conversation.conversation_id,
        user_input="export sales amount data to csv",
        thread_id=None,
    )
    print("execute:", container.runs.execute(handle.run_id), flush=True)
    run = runtime.runs.get(handle.run_id)
    print("status:", run.status, "approval:", run.approval_request_id, flush=True)
    if run.approval_request_id:
        decision = container.approvals.decide(
            run.approval_request_id,
            decision="approve",
            user_id=user.user_id,
            conversation_id=conversation.conversation_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
        )
        print("decision:", decision, flush=True)
        if decision.successor_run_id:
            print(
                "successor:",
                container.runs.resume(
                    decision.successor_run_id, approval_request_id=run.approval_request_id
                ),
                runtime.runs.get(decision.successor_run_id).final_response,
                flush=True,
            )
    container.close()
    return 0


COMMANDS = {"stream": command_stream, "repeat": command_repeat, "service": command_service}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING)
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: python -m scripts.diagnostics {{{'|'.join(sorted(COMMANDS))}}}")
        return 2
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
