"""Manually executed Evaluation Script (PROJECT_SPEC §15).

It runs the versioned Dataset through the real application over HTTP, asserts routing,
Run status, Application Events, and answer grounding, and writes a Markdown Report to
``runtime/evaluation-report.md``.

    ./scripts/dev-env.ps1 | Out-Null
    uv run python -m scripts.evaluate

Evaluation is not a CI-blocking gate; the Report records what was observed and which
limitations applied.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from scripts.evaluation_dataset import DATASET_VERSION, EVAL_DATASET, EvalCase, dataset_summary
from scripts.live_defaults import live_credentials_missing
from scripts.verify_live import live_settings

REPORT_PATH = Path("runtime/evaluation-report.md")
TRAVEL_POLICY = (
    "Enterprise Travel Policy\n\n"
    "Employees must book economy class for flights shorter than six hours.\n\n"
    "Expense reports are submitted within fourteen days of travel completion.\n\n"
    "Approval by a director is required for any single expense above 5000 USD.\n"
)


@dataclass
class CaseResult:
    case: EvalCase
    passed: bool
    observed: dict[str, object] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


class EvaluationRun:
    def __init__(self) -> None:
        from fastapi.testclient import TestClient

        from backend.api.dependencies import build_container
        from backend.api.main import create_app

        self.settings = live_settings()
        missing = live_credentials_missing()
        if missing:
            raise SystemExit(f"Missing live credentials: {', '.join(missing)}")
        self.container = build_container(self.settings)
        self.client = TestClient(create_app(self.settings, container=self.container))
        self.headers: dict[str, str] = {}
        self.conversation_id = ""
        self.document_id = ""
        # Unique per run so a knowledge assertion cannot be ranked out by stale fixtures.
        self.marker = f"evalmarker{uuid.uuid4().hex[:8]}"

    def __enter__(self) -> EvaluationRun:
        self.client.__enter__()
        login = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": self.settings.auth_bootstrap_username,
                "password": self.settings.auth_bootstrap_password.get_secret_value(),
            },
        )
        self.headers = {"Authorization": f"Bearer {login.json()['session']['token']}"}
        self.conversation_id = self.client.post(
            "/api/v1/conversations",
            json={"title": f"evaluation {DATASET_VERSION}"},
            headers=self.headers,
        ).json()["conversation_id"]
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.client.__exit__(None, None, None)
        self.container.close()

    # Setup -----------------------------------------------------------------
    def ensure_knowledge_document(self) -> str:
        """Index one evaluation document carrying this run's unique marker.

        The marker makes the Knowledge assertion independent of retrieval ranking against
        documents accumulated by earlier runs.
        """

        body = f"{TRAVEL_POLICY}\nRetrieval marker {self.marker} for this evaluation run.\n"
        upload = self.client.post(
            "/api/v1/documents",
            files={
                "file": (
                    "evaluation-travel-policy.md",
                    body.encode("utf-8"),
                    "text/markdown",
                )
            },
            headers=self.headers,
        )
        if upload.status_code != 202:
            return f"upload_failed:{upload.status_code}"
        self.document_id = upload.json()["document_id"]
        job_id = upload.json()["ingestion_job_id"]
        if job_id:
            return self._await_job(job_id)
        # A duplicate of an already known Document reuses existing state; index it if the
        # earlier attempt never promoted a version.
        document = self.client.get(
            f"/api/v1/documents/{self.document_id}", headers=self.headers
        ).json()
        if document.get("active_index_version"):
            return "reused"
        reindex = self.client.post(
            f"/api/v1/documents/{self.document_id}/reindex", headers=self.headers
        )
        if reindex.status_code != 202:
            return f"reindex_failed:{reindex.status_code}"
        return self._await_job(reindex.json()["ingestion_job_id"])

    def _await_job(self, job_id: str, *, deadline: float = 60.0) -> str:
        started = time.monotonic()
        status = "unknown"
        while time.monotonic() - started < deadline:
            job = self.client.get(f"/api/v1/ingestion-jobs/{job_id}", headers=self.headers)
            if job.status_code != 200:
                return f"lookup_failed:{job.status_code}"
            status = job.json()["status"]
            if status in {"success", "failed"}:
                break
            time.sleep(0.5)
        return status

    # Execution -------------------------------------------------------------
    def run_case(self, case: EvalCase) -> CaseResult:
        if case.family == "ingestion":
            return self._ingestion_case(case)
        accepted = self.client.post(
            f"/api/v1/conversations/{self.conversation_id}/runs",
            json={"user_input": case.user_input.format(marker=self.marker)},
            headers=self.headers,
        )
        if accepted.status_code != 202:
            if case.case_id == "routing-invalid-request" and accepted.status_code == 422:
                # Blank input is rejected at the API boundary before a Run exists.
                return CaseResult(
                    case,
                    True,
                    {
                        "post_status": accepted.status_code,
                        "error_code": accepted.json().get("error", {}).get("code"),
                    },
                )
            return CaseResult(
                case,
                False,
                {"post_status": accepted.status_code},
                [f"run was not accepted: {accepted.status_code}"],
            )
        run_id = accepted.json()["run_id"]
        events = (
            self._read_events(run_id) if case.family != "hitl" else self._read_until_pause(run_id)
        )
        state = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()
        event_types = [event["event_type"] for event in events]
        final = next((event for event in events if event["event_type"] == "final"), None)
        answer = (final or {}).get("payload", {}).get("content", "")
        observed = {
            "run_id": run_id,
            "status": state.get("status"),
            "intent": state.get("intent"),
            "routing_outcome": state.get("routing_outcome"),
            "events": event_types,
            "answer": answer[:400],
        }
        failures: list[str] = []
        hitl_events: list[str] = []
        if case.family == "hitl":
            # Decide the approval before asserting events: the waiting Run's stream may
            # close at the pause point, so the terminal event appears after the decision.
            failures.extend(self._hitl_checks(case, run_id, state, answer, observed))
            state = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()
            observed["final_status"] = state.get("status")
            hitl_events = sorted(
                set(observed.get("hitl_events", [])) | set(observed.get("rejection_events", []))
            )
            observed["hitl_events"] = hitl_events
            if "final" not in hitl_events:
                failures.append("the original Run stream has no terminal final event")
            event_types = sorted(set(event_types) | set(hitl_events))
        if (
            case.expected_routing_outcome
            and state.get("routing_outcome") != case.expected_routing_outcome
        ):
            failures.append(
                "routing_outcome "
                f"{state.get('routing_outcome')!r} != {case.expected_routing_outcome!r}"
            )
        if case.expected_intent and state.get("intent") != case.expected_intent:
            failures.append(f"intent {state.get('intent')!r} != {case.expected_intent!r}")
        for required in case.expected_events:
            if required not in event_types:
                failures.append(f"missing event {required}")
        for forbidden in case.forbidden_events:
            if forbidden in event_types:
                failures.append(f"forbidden event {forbidden}")
        lowered = answer.casefold()
        for term in case.required_answer_terms:
            if term.casefold() not in lowered:
                failures.append(f"answer missing {term!r}")
        for term in case.forbidden_answer_terms:
            if term.casefold() in lowered:
                failures.append(f"answer contains forbidden {term!r}")

        if case.family == "error":
            tool_results = [event for event in events if event["event_type"] == "tool_result"]
            observed["tool_result_statuses"] = [
                item["payload"].get("status") for item in tool_results
            ]
            if not tool_results or tool_results[0]["payload"].get("status") != "rejected":
                failures.append("unsafe SQL was not rejected")
        if case.family != "hitl" and (
            case.expected_run_status and state.get("status") != case.expected_run_status
        ):
            failures.append(f"run status {state.get('status')!r} != {case.expected_run_status!r}")
        return CaseResult(case, not failures, observed, failures)

    def _read_events(self, run_id: str, *, deadline: float = 60.0) -> list[dict]:
        events: list[dict] = []
        started = time.monotonic()
        with self.client.stream(
            "GET", f"/api/v1/runs/{run_id}/events", headers=self.headers
        ) as response:
            for line in response.iter_lines():
                if time.monotonic() - started > deadline:
                    break
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event["event_type"] in {"final", "error"}:
                        break
        return events

    def _read_until_pause(self, run_id: str, *, deadline: float = 60.0) -> list[dict]:
        """Wait for the Run to pause, then read its persisted event history."""

        started = time.monotonic()
        while time.monotonic() - started < deadline:
            status = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()[
                "status"
            ]
            if status in {"awaiting_approval", "rejected", "completed", "failed", "expired"}:
                break
            time.sleep(0.5)
        return self._read_events(run_id)

    def _hitl_checks(
        self, case: EvalCase, run_id: str, state: dict, answer: str, observed: dict
    ) -> list[str]:
        failures: list[str] = []
        approval = next(
            (
                event
                for event in self._read_events(run_id)
                if event["event_type"] == "approval_required"
            ),
            None,
        )
        observed["approval_event"] = bool(approval)
        if approval is None:
            return ["no approval_required event was emitted"]
        approval_request_id = approval["payload"]["approval_request_id"]
        correlation = {
            "conversation_id": self.conversation_id,
            "thread_id": state["thread_id"],
            "run_id": run_id,
        }
        if approval["payload"].get("approval_lifecycle") != "pending":
            failures.append("approval_required did not report a pending lifecycle")
        if state["status"] != "awaiting_approval":
            failures.append(f"waiting Run status {state['status']!r} != 'awaiting_approval'")

        if case.case_id.endswith("rejection"):
            decision = self.client.post(
                f"/api/v1/approvals/{approval_request_id}/reject",
                json=correlation,
                headers=self.headers,
            )
            observed["decision_status"] = decision.status_code
            observed["decision_body"] = decision.json() if decision.content else None
            if decision.status_code != 202:
                failures.append(f"reject returned {decision.status_code}")
            if decision.json().get("successor_run_id"):
                failures.append("reject created an execution successor")
            after = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()
            observed["final_status"] = after["status"]
            if after["status"] != "rejected":
                failures.append(f"rejected Run status {after['status']!r} != 'rejected'")
            repeat = self.client.post(
                f"/api/v1/approvals/{approval_request_id}/reject",
                json=correlation,
                headers=self.headers,
            )
            if repeat.status_code != 202:
                failures.append("repeated reject was not idempotent")
            observed["rejection_events"] = [
                event["event_type"] for event in self._read_events(run_id)
            ]
            if "final" not in observed["rejection_events"]:
                failures.append("the rejected Run has no terminal final event")
            rejected_events = self._read_events(run_id)
            if any(
                event["event_type"] == "tool_result"
                and event.get("payload", {}).get("tool_id") == "data_export"
                and event.get("payload", {}).get("status") == "success"
                for event in rejected_events
            ):
                failures.append("a rejected approval still executed the export Tool")
            final_event = next(
                (event for event in rejected_events if event["event_type"] == "final"), None
            )
            observed["rejection_answer"] = (
                (final_event or {}).get("payload", {}).get("content", "")[:200]
            )
            return failures

        decision = self.client.post(
            f"/api/v1/approvals/{approval_request_id}/approve",
            json=correlation,
            headers=self.headers,
        )
        observed["decision_status"] = decision.status_code
        if decision.status_code != 202:
            return [*failures, f"approve returned {decision.status_code}"]
        successor = decision.json().get("successor_run_id")
        observed["successor_run_id"] = successor
        if not successor:
            return [*failures, "approve did not create a successor Run"]
        original = self.client.get(f"/api/v1/runs/{run_id}", headers=self.headers).json()
        if original["status"] != "handoff_complete":
            failures.append(f"original Run status {original['status']!r} != 'handoff_complete'")
        observed["hitl_events"] = [event["event_type"] for event in self._read_events(run_id)]
        repeat = self.client.post(
            f"/api/v1/approvals/{approval_request_id}/approve",
            json=correlation,
            headers=self.headers,
        )
        if repeat.json().get("successor_run_id") != successor:
            failures.append("repeated approve returned a different successor")
        successor_events = self._read_events(successor)
        successor_final = next(
            (event for event in successor_events if event["event_type"] == "final"), None
        )
        observed["successor_events"] = [event["event_type"] for event in successor_events]
        observed["successor_answer"] = (
            (successor_final or {}).get("payload", {}).get("content", "")[:200]
        )
        if (
            not successor_final
            or "export completed" not in successor_final["payload"]["content"].casefold()
        ):
            failures.append("the successor Run did not complete the approved export")
        return failures

    def _ingestion_case(self, case: EvalCase) -> CaseResult:
        failures: list[str] = []
        observed: dict[str, object] = {}
        # Unique content keeps the lifecycle case independent of earlier runs.
        title = f"evaluation-lifecycle-{uuid.uuid4().hex[:8]}.md"
        body = f"{TRAVEL_POLICY}\nLifecycle marker {uuid.uuid4().hex}\n"
        upload = self.client.post(
            "/api/v1/documents",
            files={"file": (title, body.encode("utf-8"), "text/markdown")},
            headers=self.headers,
        )
        observed["upload_status"] = upload.status_code
        if upload.status_code != 202:
            return CaseResult(case, False, observed, ["upload did not return 202"])
        document_id = upload.json()["document_id"]
        first_version = upload.json()["index_version"]
        observed["first_job"] = self._await_job(upload.json()["ingestion_job_id"])
        document = self.client.get(f"/api/v1/documents/{document_id}", headers=self.headers).json()
        observed["active_version"] = document.get("active_index_version")
        if observed["first_job"] != "success":
            failures.append("first ingestion Job did not succeed")
        if document.get("active_index_version") != first_version:
            failures.append("the first index version was not promoted")

        reindex = self.client.post(f"/api/v1/documents/{document_id}/reindex", headers=self.headers)
        observed["reindex_status"] = reindex.status_code
        if reindex.status_code != 202:
            failures.append("reindex did not return 202")
        else:
            observed["second_job"] = self._await_job(reindex.json()["ingestion_job_id"])
            second_version = reindex.json()["index_version"]
            observed["second_version"] = second_version
            document = self.client.get(
                f"/api/v1/documents/{document_id}", headers=self.headers
            ).json()
            observed["active_version_after_reindex"] = document.get("active_index_version")
            if observed["second_job"] != "success":
                failures.append("reindex Job did not succeed")
            if document.get("active_index_version") != second_version:
                failures.append("the promoted version is not the newest successful version")
            versions = [
                item
                for item in self.client.get(
                    "/api/v1/documents?limit=50", headers=self.headers
                ).json()["items"]
                if item["document_id"] == document_id
            ]
            observed["document_visible"] = bool(versions)

        deletion = self.client.delete(f"/api/v1/documents/{document_id}", headers=self.headers)
        observed["delete_status"] = deletion.status_code
        if deletion.status_code != 202:
            failures.append("delete did not return 202")
        else:
            observed["delete_job"] = self._await_job(deletion.json()["ingestion_job_id"])
            after = self.client.get(f"/api/v1/documents/{document_id}", headers=self.headers)
            observed["document_status_after_delete"] = (
                after.json().get("status") if after.status_code == 200 else after.status_code
            )
            if after.status_code == 200 and after.json().get("status") != "deleted":
                failures.append("a deleted Document is still reported as available")
            listed = [
                item["document_id"]
                for item in self.client.get(
                    "/api/v1/documents?limit=50", headers=self.headers
                ).json()["items"]
            ]
            if document_id in listed:
                failures.append("a deleted Document is still listed as active")
            retry = self.client.post(
                f"/api/v1/ingestion-jobs/{deletion.json()['ingestion_job_id']}/retry",
                headers=self.headers,
            )
            observed["retry_of_success_status"] = retry.status_code
            if retry.status_code != 409:
                failures.append("a successful Job was retryable")
        return CaseResult(case, not failures, observed, failures)


def write_report(results: list[CaseResult], *, setup_notes: dict[str, str]) -> None:
    passed = sum(1 for result in results if result.passed and not result.skipped)
    failed = [result for result in results if not result.passed and not result.skipped]
    lines = [
        "# Evaluation Report",
        "",
        f"- Dataset version: `{DATASET_VERSION}`",
        f"- Cases: {len(results)} (passed {passed}, failed {len(failed)}, skipped "
        f"{sum(1 for result in results if result.skipped)})",
        f"- Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "## Setup",
        "",
    ]
    for key, value in setup_notes.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        ["", "## Results", "", "| Case | Family | Result | Observed |", "|---|---|---|---|"]
    )
    for result in results:
        status = "SKIP" if result.skipped else ("PASS" if result.passed else "FAIL")
        detail = (
            result.skip_reason
            if result.skipped
            else (
                "; ".join(result.failures)
                + " | observed: "
                + json.dumps(result.observed, default=str)[:400]
                if result.failures
                else json.dumps(
                    {
                        key: value
                        for key, value in result.observed.items()
                        if key
                        in {
                            "status",
                            "intent",
                            "routing_outcome",
                            "events",
                            "final_status",
                            "active_version",
                        }
                    },
                    default=str,
                )[:220]
            )
        )
        lines.append(f"| `{result.case.case_id}` | {result.case.family} | {status} | {detail} |")
    lines.extend(["", "## Known limitations", ""])
    lines.extend(
        [
            "- Evaluation is manually executed and is not a CI-blocking gate (PROJECT_SPEC §15).",
            "- Knowledge cases require the Demo travel-policy document; the setup step indexes it.",
            "- Answers in this Report were produced by the configured LLM profile for the run.",
            "- Complete production-grade E2E coverage is not an MVP gate.",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    summary = dataset_summary()
    print(
        f"Evaluation dataset {summary['dataset_version']}: "
        f"{summary['case_count']} cases {summary['families']}"
    )
    results: list[CaseResult] = []
    with EvaluationRun() as evaluation:
        setup_notes = {"knowledge_document": evaluation.ensure_knowledge_document()}
        for case in EVAL_DATASET:
            result = evaluation.run_case(case)
            results.append(result)
            state = "SKIP" if result.skipped else ("PASS" if result.passed else "FAIL")
            print(f"  [{state}] {case.case_id}: {result.failures or 'ok'}")
        setup_notes["structured_demo_data"] = "required (scripts/seed_demo.py)"
    write_report(results, setup_notes=setup_notes)
    failures = [result for result in results if not result.passed and not result.skipped]
    print(f"report: {REPORT_PATH}")
    print(f"failed: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
