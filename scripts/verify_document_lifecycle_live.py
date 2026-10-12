"""文档生命周期（ADR-040 / CC-API-003）的在线验收：走真实容器 API。

覆盖 G19/G20 的用户可见行为：

- 新上传的文档先进入 ``indexing``，摄取成功后才 ``available`` 并有 active 版本；
- 摄取失败把没有可用版本的文档置为 ``failed``；
- 重复上传报告**真实**状态（pending/running/failed/success），而不是硬编码 success；
- 失败的重建索引返回 409，而不是含糊的成功；
- 卡住/坏掉的文档可以被删除（自愈路径）。

用法（宿主，容器需在运行）：

    uv run python -m scripts.verify_document_lifecycle_live

报告写入 ``runtime/verify_document_lifecycle_report.json``（runtime/ 已被 Git 忽略）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

from backend.infrastructure.config import Settings

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REPORT_PATH = Path("runtime/verify_document_lifecycle_report.json")
JOB_DEADLINE_SECONDS = 180.0


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


def _upload(client, base_url, headers, *, filename: str, payload: bytes) -> httpx.Response:
    return client.post(
        f"{base_url}/api/v1/documents",
        headers=headers,
        files={"file": (filename, payload, "text/markdown")},
    )


def _await_job(client, base_url, headers, job_id: str) -> str:
    deadline = time.monotonic() + JOB_DEADLINE_SECONDS
    status = "pending"
    while time.monotonic() < deadline:
        response = client.get(f"{base_url}/api/v1/ingestion-jobs/{job_id}", headers=headers)
        if response.status_code == 200:
            status = str(response.json()["status"])
            if status in {"success", "failed"}:
                return status
        time.sleep(0.5)
    return status


def _document(client, base_url, headers, document_id: str) -> dict | None:
    response = client.get(f"{base_url}/api/v1/documents/{document_id}", headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Document lifecycle live acceptance.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    arguments = parser.parse_args()

    settings = Settings()
    results: dict[str, object] = {}
    failures: list[str] = []
    created: list[str] = []

    def check(name: str, condition: bool, detail: object = None) -> None:
        results[name] = {"ok": bool(condition), "detail": detail}
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}: {detail}")
        if not condition:
            failures.append(name)

    marker = uuid.uuid4().hex

    with _client() as client:
        token = _login(client, arguments.base_url, settings)
        headers = {"Authorization": f"Bearer {token}"}

        # ① 新上传 -> indexing -> promote -> available
        healthy_bytes = (
            f"Document Lifecycle Acceptance {marker}\n\n"
            "Section one explains the document lifecycle state vocabulary.\n\n"
            "Section two explains that promotion is the only path into available.\n"
        ).encode()
        created_response = _upload(
            client,
            arguments.base_url,
            headers,
            filename=f"lifecycle-{marker}.md",
            payload=healthy_bytes,
        )
        check(
            "upload_returns_202", created_response.status_code == 202, created_response.status_code
        )
        created_payload = created_response.json()
        document_id = str(created_payload["document_id"])
        created.append(document_id)
        check("new_upload_is_not_a_duplicate", created_payload["duplicate"] is False, None)

        before = _document(client, arguments.base_url, headers, document_id)
        check(
            "new_document_starts_as_indexing",
            before is not None and before["status"] == "indexing",
            before and before["status"],
        )
        check(
            "indexing_document_has_no_active_version",
            before is not None and not before.get("active_index_version"),
            before and before.get("active_index_version"),
        )

        job_status = _await_job(
            client, arguments.base_url, headers, str(created_payload["ingestion_job_id"])
        )
        check("ingestion_succeeds", job_status == "success", job_status)
        after = _document(client, arguments.base_url, headers, document_id)
        check(
            "promotion_makes_the_document_available",
            after is not None and after["status"] == "available",
            after and after["status"],
        )
        check(
            "available_document_has_the_active_version",
            after is not None
            and after.get("active_index_version") == created_payload["index_version"],
            after and after.get("active_index_version"),
        )

        # ② 重复上传（已索引）-> 真实 success
        duplicate = _upload(
            client,
            arguments.base_url,
            headers,
            filename=f"lifecycle-{marker}-again.md",
            payload=healthy_bytes,
        )
        duplicate_payload = duplicate.json()
        check(
            "duplicate_of_an_indexed_document_reports_success",
            duplicate_payload["duplicate"] is True and duplicate_payload["status"] == "success",
            {"duplicate": duplicate_payload["duplicate"], "status": duplicate_payload["status"]},
        )

        # ③ 失败摄取 -> failed（无可用版本）
        # 只有空白字符：字节数 > 0 能通过上传门控，但解析必然得到 no_extractable_text；
        # 长度随 marker 变化，确保字节唯一、不会被内容指纹去重。
        broken_bytes = (" " * (32 + len(marker))).encode("utf-8")
        broken_response = _upload(
            client,
            arguments.base_url,
            headers,
            filename=f"lifecycle-broken-{marker}.md",
            payload=broken_bytes,
        )
        check(
            "broken_upload_returns_202",
            broken_response.status_code == 202,
            broken_response.status_code,
        )
        broken_payload = broken_response.json()
        broken_id = str(broken_payload["document_id"])
        created.append(broken_id)
        broken_job = _await_job(
            client, arguments.base_url, headers, str(broken_payload["ingestion_job_id"])
        )
        check("broken_ingestion_fails", broken_job == "failed", broken_job)
        broken_document = _document(client, arguments.base_url, headers, broken_id)
        check(
            "failed_document_reports_failed_status",
            broken_document is not None and broken_document["status"] == "failed",
            broken_document and broken_document["status"],
        )
        check(
            "failed_document_has_no_active_version",
            broken_document is not None and not broken_document.get("active_index_version"),
            broken_document and broken_document.get("active_index_version"),
        )

        # ④ 重复上传（失败态）-> 真实 failed，而不是 success ← G20 核心
        broken_again = _upload(
            client,
            arguments.base_url,
            headers,
            filename=f"lifecycle-broken-{marker}-again.md",
            payload=broken_bytes,
        )
        broken_again_payload = broken_again.json()
        check(
            "duplicate_of_a_failed_document_reports_failed",
            broken_again_payload["duplicate"] is True
            and broken_again_payload["status"] == "failed",
            {
                "duplicate": broken_again_payload["duplicate"],
                "status": broken_again_payload["status"],
            },
        )

        # ⑤ 失败文档重建索引 -> 409（不是含糊成功）
        reindex = client.post(
            f"{arguments.base_url}/api/v1/documents/{broken_id}/reindex", headers=headers
        )
        check(
            "reindex_of_a_not_ready_document_is_refused",
            reindex.status_code == 409,
            {"status": reindex.status_code, "body": reindex.text[:160]},
        )

        # ⑥ 坏文档可被删除（自愈路径）
        deleted = client.delete(
            f"{arguments.base_url}/api/v1/documents/{broken_id}", headers=headers
        )
        check("broken_document_can_be_deleted", deleted.status_code == 202, deleted.status_code)
        gone = _document(client, arguments.base_url, headers, broken_id)
        check(
            "deleted_document_is_no_longer_retrievable",
            gone is None or gone["status"] == "deleted",
            gone and gone["status"],
        )
        created.remove(broken_id)

        # ⑦ 观测：历史遗留的 available + 无 active 版本行（不做数据迁移）
        listing = client.get(
            f"{arguments.base_url}/api/v1/documents", params={"limit": 100}, headers=headers
        )
        legacy = [
            item["document_id"]
            for item in listing.json()["items"]
            if item["status"] == "available" and not item.get("active_index_version")
        ]
        results["legacy_available_without_version"] = {
            "ok": True,
            "detail": {"count": len(legacy), "ids": legacy[:5]},
        }
        print(f"  [INFO] legacy available-without-version documents: {len(legacy)}")

        # 清理：删除本次创建的健康文档
        for document_id in created:
            try:
                client.delete(
                    f"{arguments.base_url}/api/v1/documents/{document_id}", headers=headers
                )
            except Exception as error:  # noqa: BLE001 - 清理失败不影响结论
                print(f"cleanup_failed: {document_id[:8]} {type(error).__name__}", file=sys.stderr)

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
