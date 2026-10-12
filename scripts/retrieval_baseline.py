"""Measure real retrieval/context ablations through the Tool execution boundary.

Run on the host without switching to the deterministic dev profile:
``python -m scripts.retrieval_baseline --mode all --out runtime/retrieval-ablation.json``
Use ``--generate`` for the hybrid production Graph. Its configured validator
is model-assisted grading, not an independent factual or completeness oracle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from backend.application.runtime import build_runtime
from backend.domain.state import initial_state
from backend.graph.main import RAGNodes, ScopedToolInvoker, build_main_graph
from backend.infrastructure.config import Settings
from backend.infrastructure.llm import create_llm_provider
from backend.infrastructure.token_budget import estimate_tokens
from backend.tools.contracts import ToolExecutionContext
from backend.tools.data_tools import KnowledgeSearchTool
from backend.tools.registry import ToolExecutor, ToolRegistry
from scripts.retrieval_dataset import RETRIEVAL_QUESTIONS, dataset_summary

MODES = ("dense", "dense_parent", "dense_bm25", "dense_rerank", "hybrid_rerank")
TOP_K = 20
FINAL_K = 6
REPORT_K = (1, 5, 10)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _write_reports(out: Path, reports: list[dict[str, Any]]) -> None:
    """Replace evidence atomically so interruption cannot leave a half JSON document."""
    temporary = out.with_name(f".{out.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps({"measurements": reports}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)


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


def retrieval_metrics(results: list[dict[str, Any]], quotes: tuple[str, ...]) -> dict[str, Any]:
    """Quote recall and unique-quote discounted gain, never outside the K window."""
    metrics: dict[str, Any] = {}
    for k in REPORT_K:
        matched, first = _match(results[:k], quotes)
        metrics[f"hit@{k}"] = bool(matched)
        metrics[f"recall@{k}"] = len(matched) / len(quotes) if quotes else None
        metrics[f"mrr@{k}"] = (1 / first if first else 0.0) if quotes else None
        seen: set[str] = set()
        gain = 0.0
        for rank, item in enumerate(results[:k], start=1):
            found, _ = _match([item], quotes)
            gain += len(found - seen) / math.log2(rank + 1)
            seen.update(found)
        # Multiple spans can legitimately be contained by a single child.
        metrics[f"quote_ndcg@{k}"] = gain / len(quotes) if quotes else None
    matched, first = _match(results, quotes)
    metrics.update(matched_quotes=sorted(matched), first_hit_rank=first)
    return metrics


class CountedAdapter:
    """Count adapter invocations; HTTP retries and billed cost remain unobserved."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: Counter[str] = Counter()
        self._lock = Lock()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self.inner, name)
        if not callable(attribute):
            return attribute

        def counted(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                self.calls[name] += 1
            return attribute(*args, **kwargs)

        return counted


def host_settings(mode: str) -> Settings:
    # Keep configured secrets/models, overriding only Compose host addresses.
    return Settings(
        mysql_host="127.0.0.1",
        mysql_port=13306,
        milvus_uri="http://127.0.0.1:19530",
        initialize_schema_on_startup=False,
        retrieval_mode=mode,
        default_retrieval_top_k=FINAL_K,
    )


def _version_fingerprint(version_record: Any) -> dict[str, Any]:
    """Return non-secret index metadata that can affect an answer batch."""
    if version_record is None:
        return {}
    return {
        "index_version": str(getattr(version_record, "index_version", "")),
        "embedding_provider": str(getattr(version_record, "embedding_provider", "")),
        "embedding_model": str(getattr(version_record, "embedding_model", "")),
        "embedding_dimension": int(getattr(version_record, "embedding_dimension", 0)),
        "collection_name": str(getattr(version_record, "collection_name", "")),
        "parser_provider": str(getattr(version_record, "parser_provider", "")),
        "status": str(getattr(version_record, "status", "")),
        "chunk_count": int(getattr(version_record, "chunk_count", 0)),
    }


def _corpus_fingerprint(corpus: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Canonicalize the exact corpus/index snapshot used by an answer batch."""
    if not corpus:
        return []
    return sorted(
        [
            {
                "document_id": str(item.get("document_id", "")),
                "index_version": str(item.get("index_version", "")),
                "source_sha256": str(item.get("source_sha256", "")),
                "version_profile": dict(item.get("version_profile") or {}),
                "chunks_sha256": str(item.get("chunks_sha256", "")),
            }
            for item in corpus
        ],
        key=lambda item: (item["document_id"], item["index_version"]),
    )


def generation_fingerprint(
    settings: Settings,
    *,
    corpus: list[dict[str, Any]] | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = [
        "scripts/retrieval_baseline.py",
        "scripts/retrieval_dataset.py",
        "backend/application/index_registry.py",
        "backend/application/runtime.py",
        "backend/infrastructure/repositories/documents.py",
        "uv.lock",
        "pyproject.toml",
        "backend/domain/state.py",
        "backend/graph/main.py",
        "backend/graph/validation.py",
        "backend/tools/data_tools.py",
        "backend/tools/registry.py",
        "backend/infrastructure/config.py",
        "backend/infrastructure/embedding.py",
        "backend/infrastructure/vector_store.py",
        "backend/infrastructure/reranker.py",
        "backend/infrastructure/token_budget.py",
        "backend/infrastructure/llm.py",
    ]
    # A broad code manifest is safer than silently missing a transitive policy dependency.
    paths = sorted(
        set(paths) | {p.relative_to(root).as_posix() for p in (root / "backend").rglob("*.py")}
    )
    return {
        "code_sha256": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths
        },
        "dataset": dataset_summary(),
        "profile": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "generation_provider": settings.llm_provider,
            "generation_model": settings.llm_model,
            "rerank_model": settings.rerank_model,
            "retrieval_mode": settings.retrieval_mode,
            "final_k": settings.default_retrieval_top_k,
            "max_context_tokens": settings.max_context_tokens,
            "max_context_chars": settings.max_context_chars,
            "rerank_enabled": settings.rerank_enabled,
            "embedding_base_url": settings.embedding_base_url,
            "llm_base_url": settings.llm_base_url,
            "local_llm_base_url": settings.local_llm_base_url,
            "llm_timeout_seconds": settings.llm_timeout_seconds,
            "llm_max_input_chars": settings.llm_max_input_chars,
            "embedding_timeout_seconds": settings.embedding_timeout_seconds,
            "embedding_batch_size": settings.embedding_batch_size,
            "milvus_timeout_seconds": settings.milvus_timeout_seconds,
            "rerank_timeout_seconds": settings.rerank_timeout_seconds,
            "rerank_top_n": settings.rerank_top_n,
            "rerank_max_document_chars": settings.rerank_max_document_chars,
            "rerank_max_total_chars": settings.rerank_max_total_chars,
            "max_parent_expansion": settings.max_parent_expansion,
            "max_retrieval_top_k": settings.max_retrieval_top_k,
            "max_citations": settings.max_citations,
            "max_tool_iterations": settings.max_tool_iterations,
            "max_repair_attempts": settings.max_repair_attempts,
            "max_answer_revisions": settings.max_answer_revisions,
        },
        "evaluator": {
            "modes": list(MODES),
            "top_k": TOP_K,
            "final_k": FINAL_K,
            "report_k": list(REPORT_K),
        },
        "corpus": _corpus_fingerprint(corpus),
        "binding": binding or {},
        "runtime": {
            "python": platform.python_version(),
            "packages": {
                name: version(name)
                for name in (
                    "pymilvus",
                    "pydantic",
                    "pydantic-settings",
                    "langgraph",
                    "langchain-core",
                    "httpx",
                    "PyMySQL",
                    "sqlglot",
                )
            },
        },
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * quantile) - 1)], 3)


def _list_available_documents(runtime: Any, *, user_id: str) -> list[Any]:
    """Read the complete authorized document set, preserving pagination semantics."""
    documents: list[Any] = []
    cursor = None
    while True:
        page, cursor = runtime.documents.list(user_id=user_id, limit=100, cursor=cursor)
        documents.extend(item for item in page if item.status == "available")
        if not cursor:
            return documents


def _snapshot_document(runtime: Any, document: Any) -> dict[str, Any]:
    """Bounded, read-only digest of original extraction and indexed child/parent rows."""
    digest = hashlib.sha256()
    cursor = ""
    count = 0
    while True:
        rows = runtime.application_pool.query_dicts(
            "SELECT chunk_id, document_id, index_version, parent_chunk_id, chunk_role, ordinal, "
            "content, location, label, section_path, block_type, page_start, page_end, "
            "content_hash, token_count FROM document_chunks "
            "WHERE document_id = %s AND index_version = %s AND chunk_id > %s "
            "ORDER BY chunk_id LIMIT 500",
            (document.document_id, document.active_index_version, cursor),
        )
        if not rows:
            break
        for row in rows:
            digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
            digest.update(b"\n")
        count += len(rows)
        cursor = rows[-1]["chunk_id"]
    source = runtime.document_contents.get(document.document_id) or ""
    return {
        "title": document.title,
        "document_id": document.document_id,
        "index_version": document.active_index_version,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "source_chars": len(source),
        "version_profile": _version_fingerprint(
            runtime.versions.get(document.active_index_version)
        ),
        "chunks_sha256": digest.hexdigest(),
        "chunk_rows": count,
    }


def _evaluation_binding(
    runtime: Any, settings: Settings
) -> tuple[Any, list[Any], list[Any], dict[str, Any]]:
    user = runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    if user is None:
        raise RuntimeError("configured evaluation user is missing")
    available = _list_available_documents(runtime, user_id=user.user_id)
    incompatible = [
        item
        for item in available
        if item.active_index_version
        and (v := runtime.versions.get(item.active_index_version))
        and (v.embedding_provider, v.embedding_model, v.embedding_dimension)
        != (settings.embedding_provider, settings.embedding_model, settings.embedding_dimension)
    ]
    incompatible.sort(key=lambda item: (not item.title.startswith("p2-probe-"), item.title))
    binding = {
        "user_id": user.user_id,
        "authorized_document_ids": sorted(item.document_id for item in available),
        "incompatible_fixture": {
            "document_id": incompatible[0].document_id,
            "index_version": incompatible[0].active_index_version,
            "version_profile": _version_fingerprint(
                runtime.versions.get(incompatible[0].active_index_version)
            ),
        }
        if incompatible
        else None,
    }
    return user, available, incompatible, binding


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [record for record in records if record["answerable"]]
    summary = {
        "answerable_count": len(answerable),
        **{
            key: _mean([float(record[key]) for record in answerable])
            for key in (
                "hit@1",
                "hit@5",
                "hit@10",
                "recall@10",
                "mrr@10",
                "quote_ndcg@10",
                "final_evidence_coverage",
                "context_quote_coverage",
            )
        },
        "latency_p50_ms": _percentile([record["duration_ms"] for record in records], 0.5),
        "latency_p95_ms": _percentile([record["duration_ms"] for record in records], 0.95),
        "degraded_count": sum(record["degraded"] for record in records),
        "boundary_passed": sum(record["boundary_passed"] is True for record in records),
        "boundary_failed": sum(record["boundary_passed"] is False for record in records),
        "tool_errors": dict(
            Counter(record["error_code"] for record in records if record["error_code"])
        ),
        "answer_factual_support": None,
        "citation_accuracy": None,
        "correct_answer_refusal": None,
        "wrong_answer_refusal": None,
    }
    return summary


def _answer_case(
    *,
    executor: ToolExecutor,
    settings: Settings,
    llm: Any,
    parents: Any,
    context: ToolExecutionContext,
    scope: list[str],
    question: Any,
    expected_error: str | None,
) -> dict[str, Any]:
    graph = build_main_graph(
        executor=ScopedToolInvoker(executor, context),
        settings=settings,
        general_llm=llm,
        context=context,
        parents=parents,
    )
    started = time.monotonic()
    final = graph.invoke(
        initial_state(
            conversation_id=context.conversation_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
            user_input=f"根据选定文档原文回答：{question.question}",
            document_scope=scope,
        )
    )
    validation = final.get("answer_validation") or {}
    status = validation.get("status")
    error = (final.get("error") or {}).get("code")
    refused = status != "validated" or bool(error)
    citations = final.get("citations") or []
    evidence = final.get("retrieval_results") or []
    known = [item.get("citation") for item in evidence]
    evidence_audit = [
        {
            "evidence_id": item.get("evidence_id") or item.get("chunk_id"),
            "chunk_id": item.get("chunk_id"),
            "document_id": item.get("document_id"),
            "index_version": item.get("index_version"),
            "content": item.get("content"),
            "citation": item.get("citation"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_path": item.get("section_path"),
        }
        for item in evidence[:FINAL_K]
    ]
    return {
        "final_answer": final.get("final_response"),
        "answer_candidate": final.get("answer_candidate"),
        "graph_question_prefix": "根据选定文档原文回答：",
        "intent": final.get("intent"),
        "routing_outcome": final.get("routing_outcome"),
        "current_path": final.get("current_path"),
        "document_scope": list(scope),
        "user_id": context.user_id,
        "authorized_document_ids": sorted(context.authorized_document_ids),
        "validation_status": status,
        "validation_reasons": validation.get("reasons"),
        "revisions": final.get("answer_revision_count"),
        "error_code": error,
        "refused": refused,
        "wrong_refusal": question.answerable and refused,
        "correct_no_answer_refusal": (
            refused and status == "insufficient_evidence"
            if question.case_kind == "no_answer"
            else None
        ),
        "boundary_passed": (
            error == expected_error and not citations and not evidence if expected_error else None
        ),
        "citation_count": len(citations),
        "required_evidence_quotes": list(question.evidence_quotes),
        "retrieved_evidence": evidence_audit,
        "final_context": final.get("context"),
        "citations": citations,
        "citation_metadata_correct": bool(citations)
        and all(citation in known for citation in citations),
        "model_assisted_support_passed": (
            status == "validated" and bool(citations) and not error
            if question.answerable and not refused
            else None
        ),
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }


def _answer_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    answers = [record["answer_evaluation"] for record in records if "answer_evaluation" in record]
    answered = [answer for answer in answers if answer["model_assisted_support_passed"] is not None]
    negatives = [answer for answer in answers if answer["correct_no_answer_refusal"] is not None]
    return {
        "question_count": len(answers),
        "pending_count": len(records) - len(answers),
        "answerable_count": sum(record["answerable"] for record in records),
        "answered_count": len(answered),
        "wrong_refusal_count": sum(answer["wrong_refusal"] for answer in answers),
        "correct_no_answer_refusal_count": sum(
            answer["correct_no_answer_refusal"] for answer in negatives
        ),
        "no_answer_count": len(negatives),
        "boundary_passed": sum(answer["boundary_passed"] is True for answer in answers),
        "boundary_failed": sum(answer["boundary_passed"] is False for answer in answers),
        "model_assisted_support_rate_on_answers": _mean(
            [float(answer["model_assisted_support_passed"]) for answer in answered]
        ),
        "citation_metadata_accuracy_on_answers": _mean(
            [float(answer["citation_metadata_correct"]) for answer in answered]
        ),
        "semantic_citation_accuracy": None,
        "latency_p50_ms": _percentile([answer["duration_ms"] for answer in answers], 0.5),
        "latency_p95_ms": _percentile([answer["duration_ms"] for answer in answers], 0.95),
        "grading": (
            "Production evidence-only LLM validator; same configured model, "
            "not an independent oracle. Metadata accuracy only checks final-context membership. "
            "No reference-answer completeness/human factual grade."
        ),
    }


def run_measurement(*, mode: str, generate: bool = False) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("unsupported ablation mode")
    settings = host_settings(mode)
    runtime = build_runtime(settings, initialize=False)
    try:
        embeddings = CountedAdapter(runtime.embeddings)
        vectors = CountedAdapter(runtime.vectors)
        reranker = CountedAdapter(runtime.reranker)
        llm = CountedAdapter(create_llm_provider(settings)) if generate else None
        tool = KnowledgeSearchTool(
            settings=settings,
            embeddings=embeddings,
            vectors=vectors,
            versions=runtime.versions,
            documents=runtime.documents,
            chunks=runtime.chunks,
            registry=runtime.registry,
            reranker=reranker,
        )
        executor = ToolExecutor(ToolRegistry([tool]))
        rag = RAGNodes(
            tools=executor, settings=settings, general_llm=None, parents=runtime.parent_context
        )
        user, available, incompatible, binding = _evaluation_binding(runtime, settings)
        titles = {item.document_id: item.title for item in available}
        corpus = [
            item
            for item in available
            if item.title in {title for q in RETRIEVAL_QUESTIONS for title in q.document_titles}
        ]
        if len(corpus) != 1:
            raise RuntimeError("the fixed complete AI313 corpus is absent or ambiguous")
        corpus_snapshot = [_snapshot_document(runtime, item) for item in corpus]
        start_fingerprint = generation_fingerprint(
            settings, corpus=corpus_snapshot, binding=binding
        )
        authorized = tuple(item.document_id for item in available)
        source = runtime.document_contents.get(corpus[0].document_id) or ""
        missing = [
            q.question_id
            for q in RETRIEVAL_QUESTIONS
            for quote in q.evidence_quotes
            if _normalize(quote) not in _normalize(source)
        ]
        if missing:
            raise RuntimeError(f"source anchors absent: {sorted(set(missing))}")
        records: list[dict[str, Any]] = []
        for index, question in enumerate(RETRIEVAL_QUESTIONS, start=1):
            scoped_ids = [item.document_id for item in corpus]
            context_ids = authorized
            expected_error = None
            if question.case_kind == "scope":
                scoped_ids = (
                    scoped_ids if question.split == "tuning" else ["missing-evaluation-document"]
                )
                context_ids = () if question.split == "tuning" else authorized
                expected_error = "document_scope_unavailable"
            elif question.case_kind == "incompatible":
                if not incompatible:
                    raise RuntimeError(
                        "no existing incompatible-profile document for boundary case"
                    )
                scoped_ids = [incompatible[0].document_id]
                expected_error = "knowledge_index_incompatible"
            context = ToolExecutionContext(
                conversation_id="retrieval-evaluation",
                thread_id="retrieval-evaluation",
                run_id=f"retrieval-{mode}-{index}",
                user_id=user.user_id,
                authorized_document_ids=context_ids,
            )
            before = {
                "embedding": sum(embeddings.calls.values()),
                "vectors": sum(vectors.calls.values()),
                "rerank": sum(reranker.calls.values()),
            }
            started = time.monotonic()
            result = executor.invoke(
                "knowledge_search",
                f"ablation-{index}",
                {"query": question.question, "top_k": TOP_K, "document_ids": scoped_ids},
                context,
            )
            results = list((result.data or {}).get("results") or [])
            # Run the same production mapping/context nodes, with fixed final K.
            state = {"retrieval_results": results[:FINAL_K]}
            state.update(rag.parent_child_mapping(state))
            final_context = rag.context_build(state)
            final_results = list(final_context.get("retrieval_results") or [])
            metrics = retrieval_metrics(results, question.evidence_quotes)
            final_matched, _ = _match(final_results, question.evidence_quotes)
            all_context_matched, _ = _match(
                [{"content": final_context.get("context")}], question.evidence_quotes
            )
            error_code = result.error.code if result.error else None
            call_delta = {
                "embedding": sum(embeddings.calls.values()) - before["embedding"],
                "vectors": sum(vectors.calls.values()) - before["vectors"],
                "rerank": sum(reranker.calls.values()) - before["rerank"],
            }
            boundary_passed = None
            if expected_error:
                boundary_passed = (
                    result.status
                    in (
                        {"failed", "rejected"}
                        if question.case_kind == "incompatible"
                        else {"rejected"}
                    )
                    and error_code == expected_error
                    and not results
                    and call_delta["vectors"] == call_delta["rerank"] == 0
                )
                if question.case_kind == "scope":
                    boundary_passed = boundary_passed and call_delta["embedding"] == 0
            records.append(
                {
                    "question_id": question.question_id,
                    "split": question.split,
                    "family": question.family,
                    "case_kind": question.case_kind,
                    "answerable": question.answerable,
                    "question": question.question,
                    "tool_status": result.status,
                    "error_code": error_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "adapter_calls": call_delta,
                    "result_count": len(results),
                    "boundary_passed": boundary_passed,
                    "degraded": any(
                        item.get("rerank_degraded") or item.get("sparse_degraded")
                        for item in results
                    ),
                    "final_evidence_coverage": len(final_matched) / len(question.evidence_quotes)
                    if question.evidence_quotes
                    else None,
                    "context_quote_coverage": len(all_context_matched)
                    / len(question.evidence_quotes)
                    if question.evidence_quotes
                    else None,
                    "context_tokens": estimate_tokens(str(final_context.get("context") or "")),
                    "parent_backgrounds_in_context": str(final_context.get("context") or "").count(
                        "[背景 | "
                    ),
                    "parent_count": len(
                        {
                            item["parent"]["chunk_id"]
                            for item in state["retrieval_results"]
                            if item.get("parent")
                        }
                    ),
                    **metrics,
                    "retrieved": [
                        {
                            "rank": rank,
                            "chunk_id": item.get("chunk_id"),
                            "index_version": item.get("index_version"),
                            "document_title": titles.get(str(item.get("document_id")), ""),
                            "page_start": item.get("page_start"),
                            "page_end": item.get("page_end"),
                            "section_path": item.get("section_path"),
                            "scores": item.get("scores"),
                            "rerank_score": item.get("rerank_score"),
                            "retrieval_mode": item.get("retrieval_mode"),
                            "rerank_degraded": item.get("rerank_degraded"),
                            "sparse_degraded": item.get("sparse_degraded"),
                        }
                        for rank, item in enumerate(results, 1)
                    ],
                }
            )
            if generate:
                records[-1]["answer_evaluation"] = _answer_case(
                    executor=executor,
                    settings=settings,
                    llm=llm,
                    parents=runtime.parent_context,
                    context=context,
                    scope=scoped_ids,
                    question=question,
                    expected_error=expected_error,
                )
            print(f"  mode={mode} {index}/60 {question.question_id} {result.status}", flush=True)
        current = runtime.documents.get(corpus[0].document_id, user_id=user.user_id)
        if current is None or current.status != "available":
            raise RuntimeError("corpus became unavailable during measurement")
        final_snapshot = [_snapshot_document(runtime, current)]
        _, _, _, final_binding = _evaluation_binding(runtime, settings)
        if (
            generation_fingerprint(settings, corpus=final_snapshot, binding=final_binding)
            != start_fingerprint
        ):
            raise RuntimeError("measurement input changed; do not accept the batch")
        return {
            "mode": mode,
            "stage": "retrieval_and_context",
            "dataset": dataset_summary(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "profile": {
                "embedding_provider": settings.embedding_provider,
                "embedding_model": settings.embedding_model,
                "embedding_dimension": settings.embedding_dimension,
                "rerank_model": settings.rerank_model,
                "generation_model": settings.llm_model,
                "generation_provider": settings.llm_provider,
                "generation_executed": generate,
                "top_k": TOP_K,
                "final_k": FINAL_K,
                "max_context_tokens": settings.max_context_tokens,
                "max_context_chars": settings.max_context_chars,
                "packages": {name: version(name) for name in ("pymilvus", "pydantic", "langgraph")},
            },
            "corpus": corpus_snapshot,
            "binding": binding,
            "measurement_fingerprint": start_fingerprint,
            "summary": _aggregate(records),
            "answer_quality": _answer_summary(records) if generate else None,
            "answer_quality_by_split": {
                split: _answer_summary([record for record in records if record["split"] == split])
                for split in ("tuning", "holdout")
            }
            if generate
            else None,
            "by_split": {
                split: _aggregate([record for record in records if record["split"] == split])
                for split in ("tuning", "holdout")
            },
            "adapter_calls": {
                "embedding": dict(embeddings.calls),
                "vectors": dict(vectors.calls),
                "rerank": dict(reranker.calls),
                "llm": dict(llm.calls) if llm else {},
            },
            "observability": {
                "billed_cost": None,
                "http_request_count": None,
                "cache_hits": None,
                "resource_usage": None,
                "note": (
                    "Adapter calls counted; no cache inserted. "
                    "Provider billing/retries and OS resource sampling not exposed."
                ),
            },
            "questions": records,
        }
    finally:
        runtime.close()


def _verify_saved_snapshot(payload: dict[str, Any], settings: Settings) -> None:
    """A matching local hash alone does not prove the live corpus is unchanged."""
    runtime = build_runtime(settings, initialize=False)
    try:
        user, _, _, binding = _evaluation_binding(runtime, settings)
        if payload.get("binding") != binding:
            raise RuntimeError("saved evaluation authorization or boundary fixture changed")
        current_corpus = []
        for source in payload.get("corpus", []):
            current = runtime.documents.get(source["document_id"], user_id=user.user_id)
            if current is None or current.status != "available":
                raise RuntimeError("saved evaluation corpus became unavailable")
            current_corpus.append(_snapshot_document(runtime, current))
        if _corpus_fingerprint(current_corpus) != _corpus_fingerprint(payload.get("corpus")):
            raise RuntimeError("saved evaluation corpus changed; start a new batch")
    finally:
        runtime.close()


def generate_answers(payload: dict[str, Any], *, reports: list[dict[str, Any]], out: Path) -> None:
    """Independent bounded Graphs; completed answers are durable after each future."""
    settings = host_settings("hybrid_rerank")
    corpus = payload.get("corpus")
    if payload.get("generation_status") == "invalid":
        raise RuntimeError("invalidated answer batch; start a new measurement batch")
    if not corpus or any(not item.get("version_profile") for item in corpus):
        raise RuntimeError("corpus index snapshot missing; start a new measurement batch")
    fingerprint = generation_fingerprint(
        settings, corpus=payload.get("corpus"), binding=payload.get("binding")
    )
    previous = payload.get("generation_fingerprint")
    has_answers = any("answer_evaluation" in record for record in payload["questions"])
    if (has_answers or previous is not None) and previous != fingerprint:
        raise RuntimeError("generation code/profile/dataset changed; do not mix answer batches")
    payload["generation_fingerprint"] = fingerprint
    payload["generation_status"] = "running"
    payload.setdefault("generation_started_at", datetime.now(UTC).isoformat())
    _write_reports(out, reports)
    runtime = build_runtime(settings, initialize=False)
    try:
        user, available, incompatible, binding = _evaluation_binding(runtime, settings)
        if payload.get("binding") != binding:
            raise RuntimeError("evaluation authorization or boundary fixture changed")
        current_corpus: list[dict[str, Any]] = []
        for source in payload["corpus"]:
            current = runtime.documents.get(source["document_id"], user_id=user.user_id)
            if (
                current is None
                or current.status != "available"
                or current.active_index_version != source["index_version"]
            ):
                raise RuntimeError("corpus changed before generation")
            current_corpus.append(_snapshot_document(runtime, current))
        if _corpus_fingerprint(current_corpus) != _corpus_fingerprint(payload.get("corpus")):
            raise RuntimeError("corpus source or index profile changed before generation")
        if generation_fingerprint(settings, corpus=current_corpus, binding=binding) != fingerprint:
            raise RuntimeError("corpus fingerprint changed before generation")
        authorized = tuple(item.document_id for item in available)

        def answer(question: Any) -> dict[str, Any]:
            embeddings = CountedAdapter(runtime.embeddings)
            vectors = CountedAdapter(runtime.vectors)
            reranker = CountedAdapter(runtime.reranker)
            llm = CountedAdapter(create_llm_provider(settings))
            executor = CountedAdapter(
                ToolExecutor(
                    ToolRegistry(
                        [
                            KnowledgeSearchTool(
                                settings=settings,
                                embeddings=embeddings,
                                vectors=vectors,
                                versions=runtime.versions,
                                documents=runtime.documents,
                                chunks=runtime.chunks,
                                registry=runtime.registry,
                                reranker=reranker,
                            )
                        ]
                    )
                )
            )
            scope = [source["document_id"] for source in payload["corpus"]]
            context_ids = authorized
            expected_error = None
            if question.case_kind == "scope":
                scope = scope if question.split == "tuning" else ["missing-evaluation-document"]
                context_ids = () if question.split == "tuning" else authorized
                expected_error = "document_scope_unavailable"
            elif question.case_kind == "incompatible":
                scope = [incompatible[0].document_id]
                expected_error = "knowledge_index_incompatible"
            context = ToolExecutionContext(
                conversation_id="answer-evaluation",
                thread_id=f"answer-{question.question_id}",
                run_id=f"answer-{question.question_id}",
                user_id=user.user_id,
                authorized_document_ids=context_ids,
            )
            result = _answer_case(
                executor=executor,
                settings=settings,
                llm=llm,
                parents=runtime.parent_context,
                context=context,
                scope=scope,
                question=question,
                expected_error=expected_error,
            )
            result["adapter_calls"] = {
                "embedding": dict(embeddings.calls),
                "vectors": dict(vectors.calls),
                "rerank": dict(reranker.calls),
                "llm": dict(llm.calls),
                "tool": dict(executor.calls),
            }
            return result

        by_id = {record["question_id"]: record for record in payload["questions"]}
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = {
                pool.submit(answer, question): question.question_id
                for question in RETRIEVAL_QUESTIONS
                if "answer_evaluation" not in by_id[question.question_id]
            }
            for future in as_completed(pending):
                question_id = pending[future]
                by_id[question_id]["answer_evaluation"] = future.result()
                payload["answer_quality"] = _answer_summary(payload["questions"])
                payload["answer_quality_by_split"] = {
                    split: _answer_summary(
                        [record for record in payload["questions"] if record["split"] == split]
                    )
                    for split in ("tuning", "holdout")
                }
                payload["profile"]["generation_executed"] = True
                payload["generation_adapter_calls"] = {
                    name: dict(
                        sum(
                            (
                                Counter(r["answer_evaluation"]["adapter_calls"][name])
                                for r in payload["questions"]
                                if "answer_evaluation" in r
                            ),
                            Counter(),
                        )
                    )
                    for name in ("embedding", "vectors", "rerank", "llm", "tool")
                }
                _write_reports(out, reports)
                completed = payload["answer_quality"]["question_count"]
                print(f"  answers {completed}/60 {question_id}", flush=True)
        print(
            "answer_quality", json.dumps(payload["answer_quality"], ensure_ascii=False), flush=True
        )
        final_corpus = []
        for source in current_corpus:
            current = runtime.documents.get(source["document_id"], user_id=user.user_id)
            if current is None or current.status != "available":
                raise RuntimeError("corpus became unavailable during generation")
            final_corpus.append(_snapshot_document(runtime, current))
        _, _, _, final_binding = _evaluation_binding(runtime, settings)
        if (
            generation_fingerprint(settings, corpus=final_corpus, binding=final_binding)
            != fingerprint
        ):
            payload["generation_status"] = "invalid"
            _write_reports(out, reports)
            raise RuntimeError("generation code changed during the answer batch")
        payload["generation_status"] = "complete"
        payload["generation_completed_at"] = datetime.now(UTC).isoformat()
        _write_reports(out, reports)
    finally:
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Real retrieval/context ablation")
    parser.add_argument("--out", default="runtime/retrieval-ablation.json")
    parser.add_argument("--mode", choices=(*MODES, "all"), default="all")
    parser.add_argument(
        "--generate", action="store_true", help="Run the production Graph for hybrid_rerank only"
    )
    parser.add_argument("--resume", action="store_true", help="Continue saved modes/answer cases")
    args = parser.parse_args()
    if args.generate and args.mode not in {"all", "hybrid_rerank"}:
        parser.error("--generate requires --mode all or --mode hybrid_rerank")
    modes = MODES if args.mode == "all" else (args.mode,)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not args.resume:
        raise RuntimeError("output already exists; choose a new batch or use --resume")
    reports = (
        json.loads(out.read_text(encoding="utf-8-sig"))["measurements"]
        if args.resume and out.exists()
        else []
    )
    for mode in modes:
        existing = next((report for report in reports if report["mode"] == mode), None)
        if existing is not None:
            expected = generation_fingerprint(
                host_settings(mode), corpus=existing.get("corpus"), binding=existing.get("binding")
            )
            if existing.get("measurement_fingerprint") != expected:
                raise RuntimeError(
                    f"saved {mode} measurement fingerprint is stale; start a new batch"
                )
            _verify_saved_snapshot(existing, host_settings(mode))
        if existing is None:
            reports.append(run_measurement(mode=mode))
        if any(report["corpus"] != reports[0]["corpus"] for report in reports):
            raise RuntimeError("corpus changed during ablation; results are not comparable")
        _write_reports(out, reports)
        current = next(report for report in reports if report["mode"] == mode)
        print(mode, json.dumps(current["summary"], ensure_ascii=False), flush=True)
    if args.generate:
        hybrid = next((report for report in reports if report["mode"] == "hybrid_rerank"), None)
        if hybrid is None:
            raise ValueError("--generate requires the hybrid_rerank mode")
        generate_answers(hybrid, reports=reports, out=out)
    print(f"raw: {out}")
    return int(any(report["summary"]["boundary_failed"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
