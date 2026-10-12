"""分片摄取的端到端验收（ADR-037）：走真实容器 API + 真实 MinerU provider。

流程：容器内 API 登录 -> 上传真实 PDF（流式落盘 + 持久 Job）-> 轮询 Job 终态 ->
宿主只读核对分片 manifest（页范围 / batch id / 复用标记 / 字符数）-> 核对
document_contents 与 child chunk 一致 -> 检索探针（真实 embedding + Milvus）。

用法（宿主，需先导出 TEST_* 凭据与 compose 运行时）：

    uv run python -m scripts.verify_sharding_live --sample tests/docs/AI-Agents-in-Depth-zh-CN.pdf

报告写入 ``runtime/verify_sharding_report.json``（runtime/ 已被 Git 忽略）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from backend.api.dependencies import build_container
from backend.infrastructure.config import Settings

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_SAMPLE = Path("tests/docs/AI-Agents-in-Depth-zh-CN.pdf")
REPORT_PATH = Path("runtime/verify_sharding_report.json")
JOB_POLL_SECONDS = 10.0


def host_settings() -> Settings:
    """宿主直跑用的档位：覆盖容器内地址，保留 .env 里的真实 provider 凭据。"""

    base = Settings()
    return base.model_copy(
        update={
            "mysql_host": "127.0.0.1",
            "mysql_port": 13306,
            "milvus_uri": "http://127.0.0.1:19530",
            "checkpoint_mode": "memory",
            "log_level": "WARNING",
        }
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _login(client: httpx.Client, base_url: str, settings: Settings) -> str:
    response = client.post(
        f"{base_url}/api/v1/auth/login",
        json={
            "username": settings.auth_bootstrap_username,
            "password": settings.auth_bootstrap_password.get_secret_value(),
        },
    )
    response.raise_for_status()
    return response.json()["session"]["token"]


def _upload(client: httpx.Client, base_url: str, token: str, sample: Path) -> dict:
    with sample.open("rb") as handle:
        response = client.post(
            f"{base_url}/api/v1/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (sample.name, handle, "application/pdf")},
            timeout=600.0,
        )
    response.raise_for_status()
    return response.json()


def _await_job(
    client: httpx.Client, base_url: str, token: str, job_id: str, *, timeout_seconds: float
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"{base_url}/api/v1/ingestion-jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        last = response.json()
        if last.get("status") in {"success", "failed"}:
            return last
        time.sleep(JOB_POLL_SECONDS)
    return {**last, "status": "timeout"}


def _probe_window(text: str, fraction: float, *, size: int = 40) -> str:
    """取一段不跨行边界的探测窗口，避免正好落在 chunk 边界上。"""

    centre = int(len(text) * fraction)
    start = max(0, min(centre - size // 2, max(0, len(text) - size)))
    newline = text.rfind("\n", 0, start)
    if newline != -1 and start - newline < size:
        start = newline + 1
    return text[start : start + size].strip()


def _shard_summary(shards) -> list[tuple]:
    return [
        (row.shard_index, row.page_start, row.page_end, row.byte_size, row.char_count, row.reused)
        for row in shards
    ]


def _collect_child_text(container, *, index_version: str, batch: int = 200) -> str:
    chunks = container.runtime.chunks
    total = chunks.count_for_version(index_version, chunk_role="child")
    pieces: list[str] = []
    for start in range(0, total, batch):
        ids = [f"{index_version}-c{index}" for index in range(start, min(start + batch, total))]
        pieces.extend(
            record.content for record in chunks.list_by_ids(ids, index_version=index_version)
        )
    return "\n".join(pieces)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sharded ingestion end-to-end acceptance.")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--job-timeout", type=float, default=1800.0)
    parser.add_argument("--skip-upload", action="store_true", help="reuse the latest document")
    parser.add_argument(
        "--purge-existing",
        action="store_true",
        help="delete a pre-existing document with the same fingerprint before uploading",
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    settings = host_settings()
    if not settings.mineru_api_key.get_secret_value():
        print("MINERU_API_KEY is not configured.", file=sys.stderr)
        return 2

    container = build_container(settings)
    results: dict[str, object] = {}
    failures: list[str] = []

    def check(name: str, condition: bool, detail: object = None) -> None:
        results[name] = {"ok": bool(condition), "detail": detail}
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}: {detail}")
        if not condition:
            failures.append(name)

    try:
        user = container.runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
        if user is None:
            print("bootstrap user is missing; run scripts/seed_demo.py first.", file=sys.stderr)
            return 2
        # trust_env=False：本机环境可能设置了 HTTP_PROXY，代理会把 127.0.0.1 的请求
        # 变成 404。验收脚本必须直连本机容器。
        with httpx.Client(timeout=600.0, trust_env=False) as client:
            token = _login(client, arguments.base_url, settings)
            check("login", bool(token))
            if arguments.purge_existing and not arguments.skip_upload:
                # P0 遗留的 313 页文档处于「available 但 active_index_version=NULL」的
                # 破损状态（G19），而重复上传会被内容指纹去重并报告 success（G20），
                # 因此必须先显式删除才能得到一次干净的真实摄取。
                fingerprint = _sha256(arguments.sample)
                stale = container.runtime.documents.find_by_fingerprint(
                    user_id=user.user_id, fingerprint=fingerprint
                )
                if stale is not None:
                    purge = container.runtime.ingestion.submit_delete(
                        user_id=user.user_id, document_id=stale.document_id
                    )
                    container.runtime.ingestion.process_job(purge.ingestion_job_id)
                    print(f"  purged stale document: {stale.document_id[:8]}")
            if arguments.skip_upload:
                documents, _ = container.runtime.documents.list(
                    user_id=user.user_id, limit=1, cursor=None
                )
                if not documents:
                    print("no document to verify.", file=sys.stderr)
                    return 2
                document_id = documents[0].document_id
                job = {}
            else:
                upload = _upload(client, arguments.base_url, token, arguments.sample)
                document_id = upload["document_id"]
                job_id = upload.get("ingestion_job_id") or ""
                if not job_id:
                    # 内容指纹命中既有文档（duplicate）：改为重建索引，
                    # 让分片链路在真实样本上实际执行一次（同时覆盖 reindex 路径）。
                    reindex = client.post(
                        f"{arguments.base_url}/api/v1/documents/{document_id}/reindex",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    reindex.raise_for_status()
                    job_id = reindex.json()["ingestion_job_id"]
                check("upload_accepted", bool(job_id), upload)
                job = _await_job(
                    client,
                    arguments.base_url,
                    token,
                    job_id,
                    timeout_seconds=arguments.job_timeout,
                )

        if job:
            check("job_succeeded", job.get("status") == "success", job.get("status"))
            if job.get("status") != "success":
                check("failure_code", bool(job.get("failure_code")), job.get("failure_code"))

        document = container.runtime.documents.get(document_id, user_id=user.user_id)
        check("document_available", document is not None and document.status == "available")
        active = document.active_index_version if document else None
        check("active_version_present", bool(active), active)

        shards = container.runtime.shards.list_for_version(active) if active else []
        ranges = [(row.page_start, row.page_end) for row in shards]
        check("shard_count", len(shards) >= 1, len(shards))
        check("shard_ranges", all(row.status == "success" for row in shards), ranges)
        check(
            "shard_batch_ids_recorded",
            all(row.provider_batch_id or row.reused for row in shards),
            [(row.provider_batch_id, row.reused) for row in shards],
        )
        check(
            "shard_chars",
            all(row.char_count > 0 for row in shards),
            [row.char_count for row in shards],
        )
        print(f"  shards: {[tuple(row)[:6] for row in _shard_summary(shards)]}")

        text = container.runtime.document_contents.get(document_id) or ""
        check("stored_text_non_empty", bool(text.strip()), len(text))

        if active:
            # 结构证据（ADR-038）：真实页码来自 provider 的 page_idx + 分片页偏移，
            # 章节路径来自标题层级；缺失时才会是 None。
            rows = container.runtime.chunks.list_by_ids(
                [
                    f"{active}-c{index}"
                    for index in range(
                        min(
                            200,
                            container.runtime.chunks.count_for_version(active, chunk_role="child"),
                        )
                    )
                ],
                index_version=active,
            )
            check(
                "chunks_carry_real_pages",
                any(row.page_start for row in rows),
                sorted({row.page_start for row in rows if row.page_start})[:5],
            )
            check(
                "chunks_carry_section_paths",
                any(row.section_path for row in rows),
                [row.section_path for row in rows if row.section_path][:2],
            )
            check(
                "chunks_carry_block_types",
                any(row.block_type for row in rows),
                sorted({row.block_type for row in rows}),
            )
            check("chunks_carry_content_hash", all(row.content_hash for row in rows), len(rows))

        if active and text:
            child_text = _collect_child_text(container, index_version=active)
            windows = {
                "head": _probe_window(text, 0.0),
                "middle": _probe_window(text, 0.5),
                "tail": _probe_window(text, 0.98),
            }
            for label, window in windows.items():
                check(f"{label}_in_children", bool(window) and window in child_text, window[:40])

            version = container.runtime.versions.get(active)
            if version is not None:
                matches = container.runtime.vectors.search(
                    collection=version.collection_name,
                    embedding=container.runtime.embeddings.embed(windows["middle"]),
                    top_k=5,
                    index_version=active,
                )
                check("vector_probe_hits", bool(matches), len(matches))
                check(
                    "vector_probe_parser",
                    version.parser_provider == "mineru",
                    version.parser_provider,
                )
    finally:
        container.close()

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
