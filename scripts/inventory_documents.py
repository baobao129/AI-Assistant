"""演示库文档清点：为「测试夹具污染」处理提供事实清单（只读，绝不删除）。

输出每个非 deleted 文档的 ID、标题、状态、owner、active 版本、创建时间、来源 Job
与版本 embedding profile，并按「正式 Demo / 测试夹具 / 历史坏行」分类。

用法（宿主）：

    uv run python -m scripts.inventory_documents

报告写入 ``runtime/document_inventory.json``（runtime/ 已被 Git 忽略）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backend.infrastructure.config import Settings
from backend.infrastructure.database import MySQLConnectionPool, PoolSettings

REPORT_PATH = Path("runtime/document_inventory.json")

# 已知的测试夹具命名模式（由 tests/ 与 scripts/ 生成）。
FIXTURE_PATTERNS = (
    "foreign.md",  # test_document_scope_live.py（他人文档夹具）
    "scope-",  # test_document_scope_live.py 上传夹具
    "lifecycle-",  # test_document_lifecycle_live.py 上传夹具
    "travel-policy-verify",  # 早期在线验收夹具
    "p2-probe-",  # P2 分片探针
    "p2_md_",  # P2 格式矩阵样本
    "p2_pdf_",  # P2 格式矩阵样本
    "retry-",  # test_ingestion_live.py
    "promote-",  # test_ingestion_live.py
    "duplicate-",  # test_ingestion_live.py
    "reindex-",  # test_ingestion_live.py
    "recovery-",  # test_ingestion_live.py
    "guard-",  # test_document_lifecycle_live.py
    "dup-fail-",  # test_document_lifecycle_live.py
    "dup-ok-",  # test_document_lifecycle_live.py
    "delete-stuck-",  # test_document_lifecycle_live.py
)

# 明确的正式 Demo 语料（demo/ 目录加载 + 真实样本）。
DEMO_SOURCES = (
    "AI-Agents-in-Depth-zh-CN.pdf",
    "尚硅谷-06-结构化输出.pdf",
    "travel-expense-policy.md",
    "procurement-playbook.md",
    "evaluation-travel-policy.md",
)


def _classify(source_name: str, status: str, has_active: bool) -> str:
    if source_name in DEMO_SOURCES:
        return "demo"
    if any(source_name.startswith(pattern) for pattern in FIXTURE_PATTERNS):
        return "fixture"
    if status == "available" and not has_active:
        return "legacy_bad_row"
    if status in {"indexing", "failed"}:
        return "stuck"
    return "unknown"


def main() -> int:
    settings = Settings(
        _env_file=None,
        mysql_host="127.0.0.1",
        mysql_port=13306,
        mysql_user=os.environ["TEST_MYSQL_USER"],
        mysql_password=os.environ["TEST_MYSQL_PASSWORD"],
        mysql_database="enterprise_agent",
    )
    pool = MySQLConnectionPool(PoolSettings.application(settings, pool_size=1))
    records: list[dict] = []
    try:
        rows = pool.query(
            "SELECT d.document_id, d.source_name, d.title, d.status, d.user_id, "
            "d.active_index_version, d.byte_size, d.created_at, "
            "(SELECT COUNT(*) FROM ingestion_jobs j WHERE j.document_id = d.document_id), "
            "(SELECT GROUP_CONCAT(v.status, ':', v.embedding_provider, '/', v.embedding_model, "
            "'/dim=', v.embedding_dimension SEPARATOR ' | ') "
            " FROM document_index_versions v WHERE v.document_id = d.document_id) "
            "FROM documents d WHERE d.status <> 'deleted' "
            "ORDER BY d.created_at DESC"
        )
        for row in rows:
            (
                document_id,
                source_name,
                title,
                status,
                user_id,
                active_version,
                byte_size,
                created_at,
                job_count,
                version_profiles,
            ) = row
            records.append(
                {
                    "document_id": str(document_id),
                    "source_name": str(source_name),
                    "title": str(title),
                    "status": str(status),
                    "user_id": str(user_id),
                    "active_index_version": str(active_version) if active_version else None,
                    "byte_size": int(byte_size),
                    "created_at": str(created_at),
                    "job_count": int(job_count),
                    "version_profiles": str(version_profiles) if version_profiles else None,
                    "classification": _classify(
                        str(source_name), str(status), active_version is not None
                    ),
                }
            )
            jobs = pool.query(
                "SELECT ingestion_job_id, index_version, job_type, status, attempt, "
                "failure_code, created_at, updated_at, finished_at FROM ingestion_jobs "
                "WHERE document_id = %s ORDER BY created_at, ingestion_job_id",
                (document_id,),
            )
            keys = (
                "ingestion_job_id",
                "index_version",
                "job_type",
                "status",
                "attempt",
                "failure_code",
                "created_at",
                "updated_at",
                "finished_at",
            )
            records[-1]["source_jobs"] = [dict(zip(keys, job, strict=True)) for job in jobs]
    finally:
        pool.close()

    buckets: dict[str, list[dict]] = {}
    for record in records:
        buckets.setdefault(record["classification"], []).append(record)

    report = {
        "total_non_deleted": len(records),
        "counts": {name: len(items) for name, items in sorted(buckets.items())},
        "records": records,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(f"非 deleted 文档：{len(records)}")
    for name, items in sorted(buckets.items()):
        print(f"  {name:16s} x{len(items)}")
    print()
    for record in records:
        active = "active" if record["active_index_version"] else "NO-ACTIVE"
        print(
            f"  [{record['classification']:14s}] {record['document_id'][:8]} "
            f"{record['status']:10s} {active:9s} jobs={record['job_count']} "
            f"{record['created_at'][:19]}  {record['source_name'][:48]}"
        )
    print(f"\nreport: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
