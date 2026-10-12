"""文档作用域（ADR-039）的在线验收：走真实容器 API + 真实 PDF/索引/LLM。

覆盖验收清单：登录、自有文档作用域提问、证据只来自选中文档、金额/数量类问题仍走
Knowledge、多文档作用域、不可用文档明确提示、伪造他人/未知 ID 的统一拒绝、
SSE 断线重连不改变作用域与最终引用。

用法（宿主，容器需在运行）：

    uv run python -m scripts.verify_document_scope_live

报告写入 ``runtime/verify_document_scope_report.json``（runtime/ 已被 Git 忽略）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from backend.infrastructure.config import Settings

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = Path("runtime/verify_document_scope_report.json")
STREAM_DEADLINE_SECONDS = 180.0

# 真实样本的 source_name 关键字（容器内已摄取）。
PRIMARY_SAMPLE = "AI-Agents-in-Depth-zh-CN.pdf"
SECONDARY_SAMPLE = "travel-expense-policy.md"


def _client() -> httpx.Client:
    # trust_env=False：本机 HTTP_PROXY 会把 127.0.0.1 的请求变成 404。
    return httpx.Client(timeout=180.0, trust_env=False)


def _login(client: httpx.Client, base_url: str, settings: Settings) -> str:
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        json={
            "username": settings.auth_bootstrap_username,
            "password": settings.auth_bootstrap_password.get_secret_value(),
        },
    )
    response.raise_for_status()
    return str(response.json()["session"]["token"])


def _documents(client: httpx.Client, base_url: str, headers: dict[str, str]) -> list[dict]:
    response = client.get(f"{base_url}/api/v1/documents", params={"limit": 100}, headers=headers)
    response.raise_for_status()
    return list(response.json()["items"])


def _start_run(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    *,
    user_input: str,
    document_ids: list[str] | None = None,
) -> tuple[int, dict]:
    created = client.post(
        f"{base_url}/api/v1/conversations", json={"title": "scope-verify"}, headers=headers
    )
    created.raise_for_status()
    conversation_id = created.json()["conversation_id"]
    body: dict[str, object] = {"user_input": user_input}
    if document_ids is not None:
        body["document_ids"] = document_ids
    response = client.post(
        f"{base_url}/api/v1/conversations/{conversation_id}/runs", json=body, headers=headers
    )
    payload = response.json()
    payload["conversation_id"] = conversation_id
    return response.status_code, payload


def _read_stream(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    run_id: str,
    *,
    last_event_id: str | None = None,
    deadline: float = STREAM_DEADLINE_SECONDS,
) -> list[dict]:
    events: list[dict] = []
    stream_headers = dict(headers)
    if last_event_id:
        stream_headers["Last-Event-ID"] = last_event_id
    started = time.monotonic()
    with client.stream(
        "GET", f"{base_url}/api/v1/runs/{run_id}/events", headers=stream_headers
    ) as response:
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


def _citations(client: httpx.Client, base_url: str, headers: dict[str, str], conversation_id: str):
    response = client.get(
        f"{base_url}/api/v1/conversations/{conversation_id}/messages",
        params={"limit": 50},
        headers=headers,
    )
    response.raise_for_status()
    for message in reversed(response.json()["items"]):
        if message["role"] == "assistant":
            return list(message.get("citations") or [])
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Document Scope live acceptance.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    arguments = parser.parse_args()

    settings = Settings()
    results: dict[str, object] = {}
    failures: list[str] = []

    def check(name: str, condition: bool, detail: object = None) -> None:
        results[name] = {"ok": bool(condition), "detail": detail}
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}: {detail}")
        if not condition:
            failures.append(name)

    with _client() as client:
        token = _login(client, arguments.base_url, settings)
        headers = {"Authorization": f"Bearer {token}"}
        documents = _documents(client, arguments.base_url, headers)
        by_name = {item["source_name"]: item for item in documents}

        primary = by_name.get(PRIMARY_SAMPLE)
        secondary = by_name.get(SECONDARY_SAMPLE)
        unusable = next(
            (
                item
                for item in documents
                if item["status"] == "available" and not item.get("active_index_version")
            ),
            None,
        )
        check(
            "fixtures_available",
            primary is not None and secondary is not None,
            {"primary": bool(primary), "secondary": bool(secondary)},
        )
        if primary is None or secondary is None:
            print("required sample documents are missing; upload them first.", file=sys.stderr)
            return 2
        primary_id, secondary_id = primary["document_id"], secondary["document_id"]

        # ① 带作用域 + 业务数据关键词：必须走 Knowledge，且引用只来自选中文档。
        status, payload = _start_run(
            client,
            arguments.base_url,
            headers,
            user_input="这份文档里提到的金额上限和数量限制分别是多少？",
            document_ids=[primary_id],
        )
        check("scoped_run_accepted", status == 202, status)
        if status == 202:
            run_id = payload["run_id"]
            _read_stream(client, arguments.base_url, headers, run_id)
            run = client.get(f"{arguments.base_url}/api/v1/runs/{run_id}", headers=headers).json()
            check(
                "scoped_business_keyword_routes_to_knowledge",
                run.get("intent") == "knowledge",
                run.get("intent"),
            )
            citations = _citations(client, arguments.base_url, headers, payload["conversation_id"])
            sources = {str(item.get("source_id")) for item in citations}
            check("scoped_citations_only_from_selection", sources <= {primary_id}, sorted(sources))
            check("scoped_citations_not_empty", bool(citations), len(citations))

        # ② 多文档作用域：引用必须落在选中的两份之内。
        status, payload = _start_run(
            client,
            arguments.base_url,
            headers,
            user_input="差旅报销的额度和这本书里讲的金额分别是什么？",
            document_ids=[primary_id, secondary_id],
        )
        check("multi_document_scope_accepted", status == 202, status)
        if status == 202:
            _read_stream(client, arguments.base_url, headers, payload["run_id"])
            citations = _citations(client, arguments.base_url, headers, payload["conversation_id"])
            sources = {str(item.get("source_id")) for item in citations}
            check(
                "multi_scope_citations_within_selection",
                sources <= {primary_id, secondary_id},
                sorted(sources),
            )

        # ③ 带作用域的寒暄：仍走 General。
        status, payload = _start_run(
            client, arguments.base_url, headers, user_input="你好", document_ids=[primary_id]
        )
        check("scoped_greeting_accepted", status == 202, status)
        if status == 202:
            _read_stream(client, arguments.base_url, headers, payload["run_id"])
            run = client.get(
                f"{arguments.base_url}/api/v1/runs/{payload['run_id']}", headers=headers
            ).json()
            check(
                "scoped_greeting_stays_general", run.get("intent") == "general", run.get("intent")
            )

        # ④ 伪造/未知 ID：统一 409，且不泄漏存在性。
        forged = "f" * 32
        status, payload = _start_run(
            client, arguments.base_url, headers, user_input="政策", document_ids=[forged]
        )
        error = payload.get("error") or {}
        check(
            "forged_id_rejected_with_scope_code",
            status == 409 and error.get("code") == "document_scope_unavailable",
            {"status": status, "code": error.get("code")},
        )
        check(
            "rejection_does_not_leak_identity",
            forged not in str(error.get("message"))
            and "not found" not in str(error.get("message")).casefold(),
            error.get("message"),
        )

        # ⑤ 可用但没有 active 版本的文档：检索边界给出明确提示，而不是「文档里没有」。
        if unusable is not None:
            status, payload = _start_run(
                client,
                arguments.base_url,
                headers,
                user_input="这份文档讲了什么？",
                document_ids=[unusable["document_id"]],
            )
            check("unusable_document_run_accepted", status == 202, status)
            if status == 202:
                events = _read_stream(client, arguments.base_url, headers, payload["run_id"])
                final = next((event for event in events if event["event_type"] == "final"), None)
                text = json.dumps(final or {}, ensure_ascii=False)
                check(
                    "unusable_document_reports_explicit_scope_message",
                    "no longer available" in text,
                    text[:200],
                )
        else:
            results["unusable_document_reports_explicit_scope_message"] = {
                "ok": None,
                "detail": "no available-without-active-version document in this corpus",
            }
            print("  [SKIP] unusable_document: no such fixture in the corpus")

        # ⑥ SSE 断线重连：带 Last-Event-ID 重放不产生第二条 final，引用保持一致。
        status, payload = _start_run(
            client,
            arguments.base_url,
            headers,
            user_input="这份文档里提到的金额上限是多少？",
            document_ids=[primary_id],
        )
        if status == 202:
            first = _read_stream(client, arguments.base_url, headers, payload["run_id"])
            finals = [event for event in first if event["event_type"] == "final"]
            last_id = str(first[-1].get("event_id")) if first else None
            replayed = _read_stream(
                client, arguments.base_url, headers, payload["run_id"], last_event_id=last_id
            )
            replayed_finals = [event for event in replayed if event["event_type"] == "final"]
            check("reconnect_yields_single_final", len(finals) == 1, len(finals))
            check(
                "reconnect_replays_at_most_one_final",
                len(replayed_finals) <= 1,
                len(replayed_finals),
            )
            citations = _citations(client, arguments.base_url, headers, payload["conversation_id"])
            sources = {str(item.get("source_id")) for item in citations}
            check("reconnect_citations_still_scoped", sources <= {primary_id}, sorted(sources))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"report: {REPORT_PATH}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
