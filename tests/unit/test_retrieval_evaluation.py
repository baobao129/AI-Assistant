import copy
import json

import pytest

from backend.infrastructure.config import Settings
from scripts.retrieval_baseline import (
    _list_available_documents,
    _match,
    generation_fingerprint,
    retrieval_metrics,
)
from scripts.retrieval_dataset import RETRIEVAL_QUESTIONS, dataset_summary


def test_fixed_dataset_has_disjoint_facts_and_quotes() -> None:
    assert dataset_summary()["splits"] == {"tuning": 30, "holdout": 30}
    assert len({q.question_id for q in RETRIEVAL_QUESTIONS}) == 60
    tuning = {
        quote for q in RETRIEVAL_QUESTIONS if q.split == "tuning" for quote in q.evidence_quotes
    }
    holdout = {
        quote for q in RETRIEVAL_QUESTIONS if q.split == "holdout" for quote in q.evidence_quotes
    }
    assert not tuning & holdout
    assert all(q.evidence_quotes for q in RETRIEVAL_QUESTIONS if q.answerable)
    assert all("sample-20" not in title for q in RETRIEVAL_QUESTIONS for title in q.document_titles)


def test_recall_at_k_does_not_count_later_results() -> None:
    results = [{"content": "无关"}] * 10 + [{"content": "证据甲"}]
    metrics = retrieval_metrics(results, ("证据甲",))
    assert metrics["recall@10"] == 0
    assert metrics["hit@10"] is False
    assert metrics["mrr@10"] == 0
    assert metrics["first_hit_rank"] == 11


def test_quote_recall_counts_all_required_spans_once() -> None:
    results = [{"content": "证据甲"}, {"content": "证据甲"}, {"content": "证据乙"}]
    metrics = retrieval_metrics(results, ("证据甲", "证据乙"))
    assert metrics["recall@1"] == 0.5
    assert metrics["recall@5"] == 1
    assert metrics["quote_ndcg@5"] == 0.75
    assert _match([{"content": "跨 页\n原文"}], ("跨页原文",))[0] == {"跨页原文"}


def test_no_answer_metrics_do_not_claim_refusal() -> None:
    metrics = retrieval_metrics([{"content": "无关"}], ())
    assert metrics["recall@10"] is None
    assert metrics["mrr@10"] is None
    assert metrics["quote_ndcg@10"] is None


def _corpus():
    return [
        {
            "document_id": "doc-1",
            "index_version": "version-1",
            "source_sha256": "a" * 64,
            "version_profile": {
                "embedding_provider": "siliconflow",
                "embedding_model": "BAAI/bge-m3",
                "embedding_dimension": 1024,
                "collection_name": "hybrid-v1",
            },
        }
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("document_id", "doc-2"),
        ("index_version", "version-2"),
        ("source_sha256", "b" * 64),
        ("version_profile", {"embedding_dimension": 64}),
        ("chunks_sha256", "c" * 64),
    ],
)
def test_generation_fingerprint_invalidates_changed_corpus(field, value):
    settings = Settings(_env_file=None)
    original = _corpus()
    changed = copy.deepcopy(original)
    changed[0][field] = value
    assert generation_fingerprint(settings, corpus=original) != generation_fingerprint(
        settings, corpus=changed
    )


def test_generation_fingerprint_ignores_batch_time_and_corpus_order():
    settings = Settings(_env_file=None)
    original = _corpus() + [{**_corpus()[0], "document_id": "doc-2"}]
    annotated = [
        {**item, "created_at": "2026-10-12", "output_dir": "elsewhere"}
        for item in reversed(original)
    ]
    assert generation_fingerprint(settings, corpus=original) == generation_fingerprint(
        settings, corpus=annotated
    )


def test_generation_fingerprint_survives_json_roundtrip():
    settings = Settings(_env_file=None)
    fingerprint = generation_fingerprint(settings, corpus=_corpus())
    persisted = json.loads(json.dumps(fingerprint, ensure_ascii=False))
    assert persisted == fingerprint


@pytest.mark.parametrize(
    "field,value",
    [
        ("rerank_top_n", 19),
        ("rerank_max_total_chars", 90000),
        ("max_answer_revisions", 0),
        ("llm_max_input_chars", 9000),
        ("max_parent_expansion", 7),
        ("max_citations", 19),
    ],
)
def test_generation_fingerprint_invalidates_execution_policy_change(field, value):
    original = Settings(_env_file=None)
    changed = Settings(_env_file=None, **{field: value})
    assert generation_fingerprint(original, corpus=_corpus()) != generation_fingerprint(
        changed, corpus=_corpus()
    )


def test_old_generation_artifact_fails_before_runtime_connection(monkeypatch, tmp_path):
    import scripts.retrieval_baseline as baseline

    monkeypatch.setattr(baseline, "host_settings", lambda mode: Settings(_env_file=None))
    monkeypatch.setattr(
        baseline, "build_runtime", lambda *a, **k: pytest.fail("must refuse before connecting")
    )
    payload = {
        "corpus": _corpus(),
        "generation_fingerprint": {"old": "fingerprint"},
        "questions": [{"answer_evaluation": {}}],
    }
    with pytest.raises(RuntimeError, match="generation code/profile/dataset"):
        baseline.generate_answers(payload, reports=[payload], out=tmp_path / "report.json")


def test_missing_index_profile_refuses_old_retrieval_batch(monkeypatch, tmp_path):
    import scripts.retrieval_baseline as baseline

    monkeypatch.setattr(baseline, "host_settings", lambda mode: Settings(_env_file=None))
    monkeypatch.setattr(
        baseline, "build_runtime", lambda *a, **k: pytest.fail("must refuse before connecting")
    )
    payload = {"corpus": [{"document_id": "doc-1", "index_version": "v1"}], "questions": []}
    with pytest.raises(RuntimeError, match="corpus index snapshot missing"):
        baseline.generate_answers(payload, reports=[payload], out=tmp_path / "report.json")


def test_main_refuses_stale_measurement_before_provider(monkeypatch, tmp_path):
    import sys

    import scripts.retrieval_baseline as baseline

    out = tmp_path / "retrieval.json"
    out.write_text(
        json.dumps({"measurements": [{"mode": "dense", "corpus": _corpus()}]}),
        encoding="utf-8",
    )
    before = out.read_bytes()
    monkeypatch.setattr(sys, "argv", ["baseline", "--mode", "dense", "--resume", "--out", str(out)])
    monkeypatch.setattr(baseline, "host_settings", lambda mode: Settings(_env_file=None))
    monkeypatch.setattr(
        baseline, "run_measurement", lambda **k: pytest.fail("must refuse before provider")
    )
    with pytest.raises(RuntimeError, match="measurement fingerprint is stale"):
        baseline.main()
    assert out.read_bytes() == before


def test_main_refuses_overwriting_old_batch(monkeypatch, tmp_path):
    import sys

    import scripts.retrieval_baseline as baseline

    out = tmp_path / "retrieval.json"
    out.write_text("old evidence", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["baseline", "--out", str(out)])
    with pytest.raises(RuntimeError, match="output already exists"):
        baseline.main()
    assert out.read_text(encoding="utf-8") == "old evidence"


def test_main_can_read_matching_fingerprint_after_json_roundtrip(monkeypatch, tmp_path):
    import sys

    import scripts.retrieval_baseline as baseline

    settings = Settings(_env_file=None)
    payload = {
        "mode": "dense",
        "corpus": _corpus(),
        "measurement_fingerprint": generation_fingerprint(settings, corpus=_corpus()),
        "summary": {"boundary_failed": 0},
    }
    out = tmp_path / "retrieval.json"
    out.write_text(json.dumps({"measurements": [payload]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["baseline", "--mode", "dense", "--resume", "--out", str(out)])
    monkeypatch.setattr(baseline, "host_settings", lambda mode: settings)
    checked = []
    monkeypatch.setattr(baseline, "_verify_saved_snapshot", lambda *args: checked.append(args))
    monkeypatch.setattr(
        baseline, "run_measurement", lambda **k: pytest.fail("must reuse matching measurement")
    )
    assert baseline.main() == 0
    assert len(checked) == 1


def test_generation_fingerprint_invalidates_authorization_or_fixture_changes():
    settings = Settings(_env_file=None)
    binding = {"user_id": "u", "authorized_document_ids": ["doc-1"], "incompatible_fixture": None}
    changed = {**binding, "authorized_document_ids": []}
    assert generation_fingerprint(
        settings, corpus=_corpus(), binding=binding
    ) != generation_fingerprint(settings, corpus=_corpus(), binding=changed)


def test_atomic_write_failure_preserves_previous_evidence(monkeypatch, tmp_path):
    from pathlib import Path

    from scripts.retrieval_baseline import _write_reports

    out = tmp_path / "retrieval.json"
    out.write_text("original evidence", encoding="utf-8")

    def interrupted(*args):
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError):
        _write_reports(out, [{"new": "evidence"}])
    assert out.read_text(encoding="utf-8") == "original evidence"
    assert list(tmp_path.iterdir()) == [out]


def test_corpus_snapshot_captures_indexed_content_changes():
    from types import SimpleNamespace

    from scripts.retrieval_baseline import _snapshot_document

    class Pool:
        content = "original child"

        def query_dicts(self, sql, parameters):
            assert parameters[:2] == ("doc", "v")
            return [{"chunk_id": "child", "content": self.content}] if not parameters[2] else []

    pool = Pool()
    runtime = SimpleNamespace(
        application_pool=pool,
        document_contents=SimpleNamespace(get=lambda document_id: "original source"),
        versions=SimpleNamespace(get=lambda version_id: None),
    )
    document = SimpleNamespace(title="PDF", document_id="doc", active_index_version="v")
    before = _snapshot_document(runtime, document)
    pool.content = "changed indexed content"
    after = _snapshot_document(runtime, document)
    assert before["source_sha256"] == after["source_sha256"]
    assert before["chunks_sha256"] != after["chunks_sha256"]
    assert before["chunk_rows"] == after["chunk_rows"] == 1


def test_evaluator_reads_all_available_documents_across_pages():
    class Runtime:
        class Documents:
            def list(self, *, user_id, limit, cursor):
                assert user_id == "user-1" and limit == 100
                if cursor is None:
                    return (
                        [
                            type("Doc", (), {"status": "available", "document_id": "a"})(),
                            type("Doc", (), {"status": "indexing", "document_id": "skip"})(),
                        ],
                        "next",
                    )
                return ([type("Doc", (), {"status": "available", "document_id": "b"})()], None)

        documents = Documents()

    assert [
        item.document_id for item in _list_available_documents(Runtime(), user_id="user-1")
    ] == ["a", "b"]


def test_counted_answer_graph_preserves_tool_authorization_context():
    from backend.infrastructure.llm import MockGeneralLLM
    from backend.tools.contracts import ToolExecutionContext
    from backend.tools.mock_tools import KnowledgeSearchTool
    from backend.tools.registry import ToolExecutor, ToolRegistry
    from scripts.retrieval_baseline import CountedAdapter, _answer_case

    seen = []

    class ScopedKnowledgeTool(KnowledgeSearchTool):
        def execute(self, tool_call_id, arguments, context=None):
            seen.append(context)
            return super().execute(tool_call_id, arguments, context)

    context = ToolExecutionContext(
        user_id="evaluation-user", authorized_document_ids=("evaluation-doc",)
    )
    executor = CountedAdapter(ToolExecutor(ToolRegistry([ScopedKnowledgeTool()])))
    _answer_case(
        executor=executor,
        settings=Settings(_env_file=None),
        llm=MockGeneralLLM(),
        parents=None,
        context=context,
        scope=["evaluation-doc"],
        question=RETRIEVAL_QUESTIONS[0],
        expected_error=None,
    )
    assert seen == [context]
    assert executor.calls["invoke"] == 1


def test_resume_refuses_changed_live_corpus_without_provider_calls(monkeypatch):
    from types import SimpleNamespace

    import scripts.retrieval_baseline as baseline

    closed = []
    runtime = SimpleNamespace(
        documents=SimpleNamespace(
            get=lambda document_id, **kwargs: SimpleNamespace(status="available")
        ),
        close=lambda: closed.append(True),
    )
    binding = {"user_id": "u"}
    monkeypatch.setattr(baseline, "build_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(
        baseline,
        "_evaluation_binding",
        lambda *args: (SimpleNamespace(user_id="u"), [], [], binding),
    )
    monkeypatch.setattr(
        baseline,
        "_snapshot_document",
        lambda *args: {**_corpus()[0], "source_sha256": "changed"},
    )
    with pytest.raises(RuntimeError, match="corpus changed"):
        baseline._verify_saved_snapshot(
            {"corpus": _corpus(), "binding": binding}, Settings(_env_file=None)
        )
    assert closed == [True]
