"""Live-database verification of the Demo path.

Runs the real application container against the Compose MySQL/Milvus runtime with the
deterministic embedding provider, then exercises authentication, Conversation/Run
creation, SSE delivery, Structured Data Tool Calling through the SQL Validator and the
read-only account, Knowledge upload/retrieval, and the HITL export approval flow.

Usage (see README "Local development"):

    uv run python -m scripts.verify_live
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from pathlib import Path

from backend.api.dependencies import build_container
from backend.api.main import create_app
from backend.infrastructure.config import Settings
from scripts.live_defaults import live_credentials_missing


def live_settings() -> Settings:
    return Settings(
        _env_file=None,
        mysql_host=os.environ.get("TEST_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.environ.get("TEST_MYSQL_PORT", "13306")),
        mysql_user=os.environ.get("TEST_MYSQL_USER", "agent_app"),
        mysql_password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
        mysql_root_password=os.environ.get("TEST_MYSQL_ROOT_PASSWORD", ""),
        mysql_readonly_user=os.environ.get("TEST_MYSQL_READONLY_USER", "agent_readonly"),
        mysql_readonly_password=os.environ.get("TEST_MYSQL_READONLY_PASSWORD", ""),
        mysql_database=os.environ.get("TEST_MYSQL_DATABASE", "enterprise_agent"),
        milvus_uri=os.environ.get("TEST_MILVUS_URI", "http://127.0.0.1:19530"),
        embedding_provider=os.environ.get("TEST_EMBEDDING_PROVIDER", "deterministic"),
        embedding_dimension=int(os.environ.get("TEST_EMBEDDING_DIMENSION", "64")),
        llm_provider=os.environ.get("TEST_LLM_PROVIDER", "extractive"),
        checkpoint_mode="memory",
        auth_session_ttl_seconds=3600,
        auth_bootstrap_username=os.environ.get("TEST_AUTH_USERNAME", "demo"),
        auth_bootstrap_password=os.environ.get("TEST_AUTH_PASSWORD", ""),
        auth_bootstrap_display_name="Demo User",
        ingestion_stale_running_seconds=60,
        ingestion_orphaned_pending_seconds=30,
        approval_expiry_seconds=600,
        sse_heartbeat_seconds=1.0,
        log_level="WARNING",
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    settings = live_settings()
    missing = live_credentials_missing()
    if missing:
        print(f"Missing live credentials: {', '.join(missing)}. Run scripts/dev-env.ps1 first.")
        return 2
    from fastapi.testclient import TestClient

    container = build_container(settings)
    app = create_app(settings, container=container)
    failures: list[str] = []
    results: dict[str, object] = {}

    def check(name: str, condition: bool, detail: object = None) -> None:
        results[name] = {"ok": bool(condition), "detail": detail}
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}: {detail}")
        if not condition:
            failures.append(name)

    with TestClient(app) as client:
        # Authentication ---------------------------------------------------
        anonymous = client.get("/api/v1/conversations")
        check(
            "unauthenticated_access_rejected", anonymous.status_code == 401, anonymous.status_code
        )
        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.auth_bootstrap_username, "password": "demo-password"},
        )
        check("login", login.status_code == 200, login.status_code)
        if login.status_code != 200:
            return 1
        token = login.json()["session"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = client.get("/api/v1/auth/me", headers=headers)
        check("current_user", me.status_code == 200, me.json().get("username"))

        created = client.post(
            "/api/v1/conversations", json={"title": "Live verification"}, headers=headers
        )
        check("conversation_created", created.status_code == 201, created.status_code)
        conversation_id = created.json()["conversation_id"]

        # Structured Data path: Tool Calling through the SQL Validator ------
        accepted = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "show sales amount by region"},
            headers=headers,
        )
        check("run_accepted", accepted.status_code == 202, accepted.json())
        if accepted.status_code == 202:
            run_id = accepted.json()["run_id"]
            stream = _read_stream(client, run_id, headers)
            event_types = [event["event_type"] for event in stream]
            check(
                "sse_has_sequence",
                [e["sequence"] for e in stream] == list(range(1, len(stream) + 1)),
                event_types,
            )
            check(
                "sse_events",
                {"agent_start", "intent", "tool_call", "tool_result", "final"} <= set(event_types),
                event_types,
            )
            final = next((e for e in stream if e["event_type"] == "final"), None)
            check(
                "structured_final",
                bool(final and final["payload"]["content"]),
                final and final["payload"]["content"][:80],
            )
            run_state = client.get(f"/api/v1/runs/{run_id}", headers=headers)
            check("run_completed", run_state.json()["status"] == "completed", run_state.json())
            check(
                "intent_structured",
                run_state.json()["intent"] == "structured_data",
                run_state.json()["intent"],
            )

        # Unsafe SQL is rejected without a database side effect ------------
        unsafe = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "drop sales table amount"},
            headers=headers,
        )
        if unsafe.status_code == 202:
            stream = _read_stream(client, unsafe.json()["run_id"], headers)
            tool_results = [e for e in stream if e["event_type"] == "tool_result"]
            check(
                "unsafe_sql_rejected",
                bool(tool_results) and tool_results[0]["payload"]["status"] == "rejected",
                tool_results,
            )

        # General path ------------------------------------------------------
        general = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "hello there"},
            headers=headers,
        )
        if general.status_code == 202:
            stream = _read_stream(client, general.json()["run_id"], headers)
            check(
                "general_path_has_no_tool_events",
                not any(e["event_type"] == "tool_call" for e in stream),
                [e["event_type"] for e in stream],
            )

        # Knowledge ingestion and retrieval --------------------------------
        # Unique content per run keeps the assertion deterministic: a near-duplicate of
        # other Demo documents can legitimately be crowded out of a small top_k window.
        marker = f"verify{uuid.uuid4().hex[:10]}"
        knowledge_text = (
            f"Enterprise Travel Policy {marker}\n\n"
            f"Employees must book economy class for flights shorter than six hours. {marker}\n\n"
            f"Expense reports are submitted within fourteen days of travel completion. {marker}\n\n"
            f"Approval by a director is required for any single expense above 5000 USD. {marker}\n"
        )
        upload = client.post(
            "/api/v1/documents",
            files={
                "file": (
                    f"travel-policy-{marker}.md",
                    knowledge_text.encode("utf-8"),
                    "text/markdown",
                )
            },
            headers=headers,
        )
        check("upload_accepted", upload.status_code == 202, upload.json())
        job_status = None
        if upload.status_code == 202:
            job_id = upload.json()["ingestion_job_id"]
            duplicate = upload.json()["duplicate"]
            if job_id:
                for _ in range(40):
                    job = client.get(f"/api/v1/ingestion-jobs/{job_id}", headers=headers).json()
                    job_status = job["status"]
                    if job_status in {"success", "failed"}:
                        break
                    _sleep(client, 0.5)
            else:
                # A duplicate of an already indexed Document reuses the active version,
                # so no new Job exists and the reported status is the reused one.
                job_status = upload.json()["status"]
            check(
                "upload_job_or_reuse",
                job_status == "success",
                {"duplicate": duplicate, "status": job_status},
            )
            check("ingestion_succeeded", job_status == "success", job_status)
            document_id = upload.json()["document_id"]
            # Read the Document directly so this assertion cannot be truncated by the
            # bounded list page.
            document = client.get(f"/api/v1/documents/{document_id}", headers=headers).json()
            if duplicate and not document.get("active_index_version"):
                # A reused Document that was never successfully indexed is re-indexed so
                # the Knowledge assertions below run against a real active version.
                reindex = client.post(f"/api/v1/documents/{document_id}/reindex", headers=headers)
                if reindex.status_code == 202:
                    reindex_job = reindex.json()["ingestion_job_id"]
                    for _ in range(40):
                        job = client.get(
                            f"/api/v1/ingestion-jobs/{reindex_job}", headers=headers
                        ).json()
                        job_status = job["status"]
                        if job_status in {"success", "failed"}:
                            break
                        _sleep(client, 0.5)
                    document = client.get(
                        f"/api/v1/documents/{document_id}", headers=headers
                    ).json()
            check(
                "active_index_version_promoted",
                bool(document.get("active_index_version")),
                document.get("active_index_version"),
            )
            knowledge = client.post(
                f"/api/v1/conversations/{conversation_id}/runs",
                json={
                    "user_input": (
                        f"What does the travel policy document say about {marker} flights?"
                    )
                },
                headers=headers,
            )
            if knowledge.status_code == 202:
                stream = _read_stream(client, knowledge.json()["run_id"], headers)
                event_types = [e["event_type"] for e in stream]
                check("knowledge_retrieval_event", "retrieval" in event_types, event_types)
                final = next((e for e in stream if e["event_type"] == "final"), None)
                check(
                    "knowledge_answer_grounded",
                    bool(
                        final
                        and "insufficient evidence" not in final["payload"]["content"].casefold()
                    ),
                    final and final["payload"]["content"][:100],
                )

        # Structured Data inspection APIs ----------------------------------
        sources = client.get("/api/v1/data-sources", headers=headers).json()
        check("data_sources_listed", sources["items"][0]["table_count"] >= 3, sources)
        tables = client.get(
            f"/api/v1/data-sources/{settings.data_source_id}/tables", headers=headers
        ).json()
        check(
            "tables_listed", len(tables["items"]) >= 3, [t["table_name"] for t in tables["items"]]
        )
        preview = client.get(
            f"/api/v1/data-sources/{settings.data_source_id}/tables/sales_orders/preview",
            headers=headers,
        )
        check(
            "preview_bounded",
            preview.status_code == 200 and preview.json()["row_count"] <= 50,
            preview.json().get("row_count"),
        )
        over = client.get(
            f"/api/v1/data-sources/{settings.data_source_id}/tables/sales_orders/preview?limit=1000",
            headers=headers,
        )
        check("preview_limit_enforced", over.status_code == 422, over.status_code)
        unknown_table = client.get(
            f"/api/v1/data-sources/{settings.data_source_id}/tables/secret_table/preview",
            headers=headers,
        )
        check(
            "unknown_table_not_found", unknown_table.status_code == 404, unknown_table.status_code
        )

        # HITL export approval ---------------------------------------------
        export = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"user_input": "export sales amount data to csv"},
            headers=headers,
        )
        if export.status_code == 202:
            run_id = export.json()["run_id"]
            # The waiting Run stays awaiting_approval. Read the persisted Run Event
            # history through the replay path instead of holding a live stream open
            # across the human decision.
            run_state = _await_status(
                client, run_id, headers, {"awaiting_approval", "failed", "completed"}
            )
            stream = _replay_events(client, run_id, headers)
            approval = next((e for e in stream if e["event_type"] == "approval_required"), None)
            check(
                "approval_required_event", approval is not None, [e["event_type"] for e in stream]
            )
            if approval:
                approval_request_id = approval["payload"]["approval_request_id"]
                check(
                    "waiting_run_awaiting_approval",
                    run_state["status"] == "awaiting_approval",
                    run_state["status"],
                )
                decision = client.post(
                    f"/api/v1/approvals/{approval_request_id}/approve",
                    json={
                        "conversation_id": conversation_id,
                        "thread_id": run_state["thread_id"],
                        "run_id": run_id,
                    },
                    headers=headers,
                )
                check("approve_accepted", decision.status_code == 202, decision.json())
                if decision.status_code == 202:
                    successor = decision.json()["successor_run_id"]
                    check("successor_created", bool(successor), successor)
                    original = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
                    check(
                        "original_handoff_complete",
                        original["status"] == "handoff_complete",
                        original["status"],
                    )
                    repeat = client.post(
                        f"/api/v1/approvals/{approval_request_id}/approve",
                        json={
                            "conversation_id": conversation_id,
                            "thread_id": run_state["thread_id"],
                            "run_id": run_id,
                        },
                        headers=headers,
                    )
                    check("duplicate_approve_idempotent", repeat.status_code == 202, repeat.json())
                    if successor:
                        successor_stream = _read_stream(client, successor, headers)
                        check(
                            "successor_stream_terminal",
                            any(e["event_type"] in {"final", "error"} for e in successor_stream),
                            [e["event_type"] for e in successor_stream],
                        )
                        successor_final = next(
                            (e for e in successor_stream if e["event_type"] == "final"), None
                        )
                        successor_state = client.get(
                            f"/api/v1/runs/{successor}", headers=headers
                        ).json()
                        check(
                            "successor_export_completed",
                            bool(
                                successor_final
                                and "export completed"
                                in successor_final["payload"]["content"].casefold()
                            ),
                            successor_state["status"],
                        )
                        # The approved artifact is bound to the approval request, so a
                        # repeated decision cannot create a second export.
                        replay = client.post(
                            f"/api/v1/approvals/{approval_request_id}/approve",
                            json={
                                "conversation_id": conversation_id,
                                "thread_id": run_state["thread_id"],
                                "run_id": run_id,
                            },
                            headers=headers,
                        )
                        check(
                            "duplicate_approve_same_successor",
                            replay.json().get("successor_run_id") == successor,
                            replay.json(),
                        )

    container.close()
    report = {"failures": failures, "results": results}
    print(json.dumps(report, indent=2, default=str))
    Path("runtime/verify-live-report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return 1 if failures else 0


def _replay_events(client, run_id: str, headers: dict[str, str], *, limit: int = 400) -> list[dict]:
    """Read the retained Run Event history through the SSE replay path."""

    events: list[dict] = []
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
        if response.status_code != 200:
            return events
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            events.append(json.loads(line[6:]))
            if events[-1]["event_type"] in {"final", "error"} or len(events) >= limit:
                break
    return events


def _await_status(
    client,
    run_id: str,
    headers: dict[str, str],
    statuses: set[str],
    *,
    deadline: float = 60.0,
) -> dict:
    """Poll Run status until it reaches one of ``statuses`` or the deadline passes."""

    import time

    started = time.monotonic()
    state = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    while state.get("status") not in statuses and time.monotonic() - started < deadline:
        time.sleep(0.5)
        state = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    return state


def _read_stream(
    client,
    run_id: str,
    headers: dict[str, str],
    *,
    limit: int = 400,
    deadline: float = 90.0,
) -> list[dict]:
    """Read one Run's live SSE stream until a terminal event or the bounded deadline."""

    import time

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
            if events[-1]["event_type"] in {"final", "error"} or len(events) >= limit:
                break
    return events


def _sleep(client, seconds: float) -> None:
    import time

    time.sleep(seconds)


if __name__ == "__main__":
    sys.exit(main())
