"""Explicit V1 selections; seven integration cases, no E2E discovery."""

UNIT_TESTS = [
    "tests/unit/test_v1_closure.py",
    "tests/unit/test_evidence_validation.py::test_semantic_judge_cannot_override_wrong_numbers_or_direct_negation",
    "tests/unit/test_evidence_validation.py::test_semantic_judge_fails_closed_on_malformed_or_unknown_source",
    "tests/unit/test_retrieval_evaluation.py::test_generation_fingerprint_survives_json_roundtrip",
    "tests/unit/test_retrieval_evaluation.py::test_counted_answer_graph_preserves_tool_authorization_context",
    "tests/unit/test_rag_parent_context.py::test_a_huge_parent_cannot_displace_other_evidence",
    "tests/unit/test_structure_chunking.py::test_section_path_follows_the_heading_stack",
    "tests/unit/test_pagination_cursor.py::test_aware_cursor_converts_the_instant_to_utc[8]",
    "tests/unit/test_pagination_cursor.py::test_each_list_reaches_the_next_page_without_timezone_loss",
]

INTEGRATION_TESTS = [
    "tests/integration/test_v1_closure_pipeline.py::test_grading_uses_original_pages_and_separate_invocation",
    "tests/integration/test_v1_closure_pipeline.py::test_repaired_chapter_metadata_reaches_graph_citations",
    "tests/integration/test_v1_closure_pipeline.py::test_cross_chapter_answer_uses_only_selected_bounded_evidence",
    "tests/integration/test_hybrid_live.py::test_native_bm25_filters_all_authorized_versions_before_global_rank",
    "tests/integration/test_audit_boundaries_live.py::test_explicit_versions_reject_before_any_provider_and_recheck_scope",
    "tests/integration/test_resume_state.py::test_resume_preserves_checkpoint_state_budgets_and_execution_facts",
    "tests/integration/test_visible_stream_graph.py::test_custom_stream_is_incremental_before_final_without_hidden_state",
]


def selection(kind: str) -> list[str]:
    nodes = UNIT_TESTS if kind == "unit" else INTEGRATION_TESTS
    if kind not in {"unit", "integration"}:
        raise ValueError("unknown focused suite")
    if not nodes or any(not n.startswith(f"tests/{kind}/") or "e2e" in n for n in nodes):
        raise ValueError("focused test selection must not discover E2E")
    if kind == "integration" and len(nodes) != 7:
        raise ValueError("V1 integration suite must contain exactly seven explicit cases")
    return list(nodes)
