"""检索质量基线与消融测量：Hit@K / Recall@K / MRR / 拒答分数分布。

设计要点：

- 复用真实检索路径（``knowledge_search`` 的 Tool 实现 + Registry/授权），不复制检索逻辑，
  也不与 ``scripts/evaluate.py`` 的行为断言重复 —— 后者测"行为是否正确"，本脚本测"检索质量"。
- 证据真值按**原文锚点**匹配（不绑定 chunk ID），因此 P3/P4 更换分块或引入 Hybrid 后，
  同一问题集仍可比（POST_MVP_DEVELOPMENT_PLAN §6）。
- 输出 JSON 含语料快照、每题候选明细与聚合指标，作为发布报告与消融对比的原始证据。

用法（宿主运行；将 ``MILVUS_URI`` 指向宿主端口，避免容器主机名）：

```powershell
$env:MILVUS_URI='http://127.0.0.1:19530'
uv run python -m scripts.retrieval_baseline --out docs/POST_MVP_BASELINE_DENSE.json
```
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.application.runtime import build_runtime
from backend.infrastructure.config import Settings
from backend.tools.contracts import ToolExecutionContext
from backend.tools.data_tools import KnowledgeSearchInput, KnowledgeSearchTool
from scripts.retrieval_dataset import (
    RETRIEVAL_DATASET_VERSION,
    RETRIEVAL_QUESTIONS,
    dataset_summary,
)

TOP_K = 20
REPORT_K = (1, 5, 10)


def _normalize(value: str) -> str:
    """匹配前折叠全部空白：PDF 解析结果常含额外空格/换行，锚点仍按原文字符匹配。"""

    return re.sub(r"\s+", "", value)


def _match(results: list[dict[str, Any]], quotes: tuple[str, ...]) -> tuple[set[str], int | None]:
    matched: set[str] = set()
    first_rank: int | None = None
    for rank, item in enumerate(results, start=1):
        content = _normalize(str(item.get("content") or ""))
        for quote in quotes:
            if quote and _normalize(quote) in content:
                matched.add(quote)
                if first_rank is None:
                    first_rank = rank
    return matched, first_rank


def _score_mean(values: list[float], count: int) -> float | None:
    window = values[:count]
    if not window:
        return None
    return round(sum(window) / len(window), 6)


def run_measurement(*, mode: str) -> dict[str, Any]:
    settings = Settings()
    runtime = build_runtime(settings)
    try:
        tool = KnowledgeSearchTool(
            settings=settings,
            embeddings=runtime.embeddings,
            vectors=runtime.vectors,
            versions=runtime.versions,
            documents=runtime.documents,
            chunks=runtime.chunks,
            registry=runtime.registry,
        )
        user = runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
        if user is None:
            raise RuntimeError("demo user is missing; run scripts.seed_demo first")
        documents, _ = runtime.documents.list(user_id=user.user_id, limit=100, cursor=None)
        available = [item for item in documents if item.status == "available"]
        titles = {item.document_id: item.title for item in available}
        context = ToolExecutionContext(
            conversation_id="retrieval-baseline",
            thread_id="retrieval-baseline",
            run_id="retrieval-baseline",
            user_id=user.user_id,
            authorized_document_ids=tuple(item.document_id for item in available),
        )

        records: list[dict[str, Any]] = []
        failures: list[str] = []
        for index, question in enumerate(RETRIEVAL_QUESTIONS, start=1):
            result = tool.execute(
                f"baseline-{index}",
                KnowledgeSearchInput(query=question.question, top_k=TOP_K),
                context,
            )
            results: list[dict[str, Any]] = list((result.data or {}).get("results") or [])
            scores = [float(item.get("score") or 0.0) for item in results]
            matched, first_rank = _match(results, question.evidence_quotes)
            hits = {k: bool(first_rank is not None and first_rank <= k) for k in REPORT_K}
            recall_at_10 = (
                round(len(matched) / len(question.evidence_quotes), 4)
                if question.evidence_quotes
                else None
            )
            record: dict[str, Any] = {
                "question_id": question.question_id,
                "split": question.split,
                "family": question.family,
                "question": question.question,
                "answerable": question.answerable,
                "tool_status": result.status,
                "result_count": len(results),
                "top1_score": round(scores[0], 6) if scores else None,
                "top3_mean_score": _score_mean(scores, 3),
                "top5_mean_score": _score_mean(scores, 5),
                "matched_quotes": sorted(matched),
                "missing_quotes": sorted(set(question.evidence_quotes) - matched),
                "first_hit_rank": first_rank,
                "recall_at_10": recall_at_10,
                "mrr_at_10": (
                    round(1.0 / first_rank, 4)
                    if first_rank is not None and first_rank <= 10
                    else (0.0 if question.evidence_quotes else None)
                ),
                "retrieved": [
                    {
                        "rank": rank,
                        "chunk_id": item.get("chunk_id"),
                        "document_title": titles.get(str(item.get("document_id") or ""), ""),
                        "label": (item.get("metadata") or {}).get("label"),
                        "location": (item.get("metadata") or {}).get("location"),
                        "score": item.get("score"),
                    }
                    for rank, item in enumerate(results, start=1)
                ],
            }
            record.update({f"hit@{k}": hits[k] for k in REPORT_K})
            if question.answerable and not hits[10]:
                failures.append(question.question_id)
            records.append(record)

        answerable = [record for record in records if record["answerable"]]
        unanswerable = [record for record in records if not record["answerable"]]
        summary: dict[str, Any] = {
            "answerable_count": len(answerable),
            "hit@1": _ratio(answerable, "hit@1"),
            "hit@5": _ratio(answerable, "hit@5"),
            "hit@10": _ratio(answerable, "hit@10"),
            "mean_recall@10": _mean(
                [
                    record["recall_at_10"]
                    for record in answerable
                    if record["recall_at_10"] is not None
                ]
            ),
            "mrr@10": _mean(
                [record["mrr_at_10"] for record in answerable if record["mrr_at_10"] is not None]
            ),
            "failures": failures,
            "unanswerable_top1_scores": [
                {"question_id": record["question_id"], "top1_score": record["top1_score"]}
                for record in unanswerable
            ],
        }
        by_split: dict[str, Any] = {}
        for split in ("tuning", "holdout"):
            subset = [record for record in answerable if record["split"] == split]
            by_split[split] = {
                "count": len(subset),
                "hit@10": _ratio(subset, "hit@10"),
                "mean_recall@10": _mean(
                    [
                        record["recall_at_10"]
                        for record in subset
                        if record["recall_at_10"] is not None
                    ]
                ),
            }
        return {
            "mode": mode,
            "dataset": dataset_summary(),
            "dataset_version": RETRIEVAL_DATASET_VERSION,
            "recorded_at": datetime.now(UTC).isoformat(),
            "profile": {
                "embedding_provider": settings.embedding_provider,
                "embedding_model": settings.embedding_model,
                "embedding_dimension": settings.embedding_dimension,
                "top_k": TOP_K,
            },
            "corpus": [
                {
                    "title": item.title,
                    "document_id": item.document_id,
                    "index_version": item.active_index_version,
                }
                for item in available
            ],
            "summary": summary,
            "by_split": by_split,
            "questions": records,
        }
    finally:
        runtime.close()


def _ratio(records: list[dict[str, Any]], key: str) -> float | None:
    if not records:
        return None
    return round(sum(1 for record in records if record[key]) / len(records), 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="测量当前检索链路的 Hit@K / Recall@K / MRR")
    parser.add_argument("--out", default="docs/POST_MVP_BASELINE_DENSE.json")
    parser.add_argument("--mode", default="dense-current")
    args = parser.parse_args()

    payload = run_measurement(mode=args.mode)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = payload["summary"]
    print(
        f"mode={payload['mode']} dataset={payload['dataset_version']} "
        f"corpus={len(payload['corpus'])}"
    )
    print(
        f"answerable={summary['answerable_count']} "
        f"hit@1={summary['hit@1']} hit@5={summary['hit@5']} hit@10={summary['hit@10']} "
        f"recall@10={summary['mean_recall@10']} mrr@10={summary['mrr@10']}"
    )
    print("by_split:", json.dumps(payload["by_split"], ensure_ascii=False))
    if summary["failures"]:
        print("failures:", ", ".join(summary["failures"]))
    if summary["unanswerable_top1_scores"]:
        print(
            "unanswerable_top1:",
            json.dumps(summary["unanswerable_top1_scores"], ensure_ascii=False),
        )
    print(f"raw: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
