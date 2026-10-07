"""用于人工执行评估的分版本评估 Dataset。

PROJECT_SPEC §15 要求一个分版本的 Dataset、一个人工执行的 Script 和一个 Report，
覆盖 Knowledge 接地/引用、Structured Data 保真度、General 行为、
routing、错误处理与 HITL 状态迁移。本模块定义该 Dataset。

每个用例声明期望的 routing 结果、期望的终态 Run 状态，以及必须出现在
Run 的 Application Events 中的证据。``expected`` 值是 Report 中的断言，
而非评分启发式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DATASET_VERSION = "eval-1"

EvalFamily = Literal[
    "knowledge",
    "structured_data",
    "general",
    "routing",
    "error",
    "hitl",
    "ingestion",
]


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    family: EvalFamily
    title: str
    user_input: str
    expected_intent: str | None
    expected_routing_outcome: str
    expected_run_status: str
    expected_events: tuple[str, ...] = ()
    forbidden_events: tuple[str, ...] = ()
    required_answer_terms: tuple[str, ...] = ()
    forbidden_answer_terms: tuple[str, ...] = ()
    notes: str = ""
    setup: dict[str, str] = field(default_factory=dict)


EVAL_DATASET: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="knowledge-grounded-answer",
        family="knowledge",
        title="Knowledge question is answered from indexed evidence with citations",
        user_input=(
            "What does the travel policy document say about economy class flights for {marker}?"
        ),
        expected_intent="knowledge",
        expected_routing_outcome="routed",
        expected_run_status="completed",
        expected_events=("agent_start", "intent", "retrieval", "citation", "final"),
        forbidden_events=("tool_call",),
        required_answer_terms=("economy",),
        notes=(
            "Requires the evaluation travel-policy document from setup. The query carries "
            "the per-run marker so the assertion does not depend on retrieval ranking "
            "against accumulated Demo documents."
        ),
    ),
    EvalCase(
        case_id="knowledge-insufficient-evidence",
        family="knowledge",
        title="Knowledge question without matching evidence returns insufficient evidence",
        user_input="What does the orbital launch manual say about fuel mixtures?",
        expected_intent="knowledge",
        expected_routing_outcome="routed",
        expected_run_status="completed",
        expected_events=("agent_start", "intent", "final"),
        required_answer_terms=("insufficient evidence",),
        notes="The answer must never be filled from pretrained knowledge.",
    ),
    EvalCase(
        case_id="structured-query-fidelity",
        family="structured_data",
        title="Structured Data answer is grounded in bounded query results",
        user_input="show sales amount by region",
        expected_intent="structured_data",
        expected_routing_outcome="routed",
        expected_run_status="completed",
        expected_events=("agent_start", "intent", "tool_call", "tool_result", "final"),
        required_answer_terms=("rows",),
    ),
    EvalCase(
        case_id="structured-unsafe-sql",
        family="error",
        title="Unsafe SQL is rejected by the Validator without a database side effect",
        user_input="drop sales table amount",
        expected_intent="structured_data",
        expected_routing_outcome="routed",
        expected_run_status="completed",
        expected_events=("tool_call", "tool_result"),
        required_answer_terms=("rejected", "not executed"),
        notes="The Tool Result status must be rejected with sql_rejected.",
    ),
    EvalCase(
        case_id="general-no-enterprise-tools",
        family="general",
        title="General conversation never calls an enterprise Tool",
        user_input="hello there",
        expected_intent="general",
        expected_routing_outcome="routed",
        expected_run_status="completed",
        expected_events=("agent_start", "intent", "final"),
        forbidden_events=("tool_call", "retrieval"),
    ),
    EvalCase(
        case_id="routing-invalid-request",
        family="routing",
        title="Blank request input is rejected at the API boundary without creating a Run",
        user_input="   ",
        expected_intent=None,
        expected_routing_outcome="",
        expected_run_status="",
        notes=(
            "`invalid_request` is a deterministic Router outcome, but the API validates blank "
            "input first and returns 422; the evaluation records that observable boundary."
        ),
    ),
    EvalCase(
        case_id="routing-mixed-unsupported",
        family="routing",
        title="Mixed Knowledge and Structured Data request is explicitly unsupported",
        user_input="compare the policy document with the sales amount figures",
        expected_intent=None,
        expected_routing_outcome="unsupported_mixed_query",
        expected_run_status="completed",
        expected_events=("agent_start", "final"),
        forbidden_events=("tool_call", "retrieval"),
        required_answer_terms=("unsupported",),
    ),
    EvalCase(
        case_id="routing-error-safe-outcome",
        family="routing",
        title="Routing error produces a safe terminal outcome",
        user_input="__routing_error__ explore the data",
        expected_intent=None,
        expected_routing_outcome="routing_error",
        expected_run_status="completed",
        expected_events=("agent_start", "final"),
        forbidden_events=("tool_call",),
        required_answer_terms=("routed safely",),
    ),
    EvalCase(
        case_id="hitl-export-approval",
        family="hitl",
        title="Export requires approval before any side effect, then completes in a successor Run",
        user_input="export sales amount data to csv",
        expected_intent="structured_data",
        expected_routing_outcome="routed",
        expected_run_status="handoff_complete",
        expected_events=("intent", "tool_call", "tool_result", "approval_required"),
        notes=(
            "The waiting Run must be awaiting_approval, Approve must create a successor Run "
            "that completes, and a repeated Approve must return the same successor."
        ),
    ),
    EvalCase(
        case_id="hitl-export-rejection",
        family="hitl",
        title="Rejected export creates no successor and no artifact",
        user_input="export sales order data to csv",
        expected_intent="structured_data",
        expected_routing_outcome="routed",
        expected_run_status="rejected",
        expected_events=("intent", "tool_call", "approval_required", "final"),
        notes="Reject must be idempotent and must not execute the Tool.",
    ),
    EvalCase(
        case_id="ingestion-lifecycle",
        family="ingestion",
        title="Document upload, indexing, reindex, and delete keep active-version correctness",
        user_input="",
        expected_intent=None,
        expected_routing_outcome="",
        expected_run_status="",
        notes=(
            "Upload returns 202 with a pending Job; the Job becomes success; the active "
            "index version is promoted; reindex creates a new version without retiring the "
            "old one before promotion; delete removes the Document from active retrieval."
        ),
    ),
)


def dataset_summary() -> dict[str, object]:
    families: dict[str, int] = {}
    for case in EVAL_DATASET:
        families[case.family] = families.get(case.family, 0) + 1
    return {
        "dataset_version": DATASET_VERSION,
        "case_count": len(EVAL_DATASET),
        "families": families,
    }
