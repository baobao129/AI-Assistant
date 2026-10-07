"""意图感知的 Answer Validation（Graph-008）。

Validation 在路径执行之后、``response_finalize`` 提交终态响应之前运行。确定性的安全
Routing Outcome 会跳过意图感知的校验。Revision 对单个答案候选受 ``max_answer_revisions``
约束，绝不会变成无界的自循环。
"""

from __future__ import annotations

from typing import Literal, Protocol

from backend.domain.state import GraphState
from backend.graph.policies import ExecutionBudgets

ValidationStatus = Literal[
    "validated",
    "retryable_validation_failure",
    "insufficient_evidence",
    "invalid_result",
    "validation_error",
]

REVISION_ELIGIBLE_INTENTS = ("knowledge", "structured_data")

MAX_ANSWER_CHARS = 20000
MIN_ANSWER_CHARS = 1


class AnswerValidator(Protocol):
    def __call__(self, state: GraphState, budgets: ExecutionBudgets) -> dict[str, object]: ...


def validate_answer(
    state: GraphState, budgets: ExecutionBudgets | None = None
) -> dict[str, object]:
    # revision 预算由 Graph 路由谓词读取，此处不消费。
    del budgets
    intent = state.get("intent")
    candidate = (state.get("answer_candidate") or "").strip()
    revisions = int(state.get("answer_revision_count") or 0)

    if state.get("routing_outcome") not in (None, "routed"):
        # 确定性的安全终态是校验的合法例外，直接视为 validated。
        return {"answer_validation": {"status": "validated", "reasons": ["safe_routing_outcome"]}}

    if not candidate:
        return _outcome("invalid_result", ["empty_answer"], revisions)
    if len(candidate) > MAX_ANSWER_CHARS:
        return _outcome("invalid_result", ["answer_too_long"], revisions)
    if len(candidate) < MIN_ANSWER_CHARS:
        return _outcome("invalid_result", ["answer_too_short"], revisions)

    if intent == "knowledge":
        return _validate_knowledge(state, candidate, revisions)
    if intent == "structured_data":
        return _validate_structured(state, candidate, revisions)
    if intent == "general":
        return _validate_general(state, candidate, revisions)
    return _outcome("validation_error", ["unknown_intent"], revisions)


def _validate_knowledge(state: GraphState, candidate: str, revisions: int) -> dict[str, object]:
    # 知识路径缺少已授权证据时返回 insufficient_evidence 安全终态，绝不使用
    # 预训练知识填补企业事实。
    results = state.get("retrieval_results") or []
    if not results:
        return _outcome("insufficient_evidence", ["no_authorized_evidence"], revisions)
    if not state.get("context"):
        return _outcome("insufficient_evidence", ["empty_context"], revisions)
    lowered = candidate.casefold()
    if "insufficient evidence" in lowered:
        return _outcome(
            "insufficient_evidence", ["model_reported_insufficient_evidence"], revisions
        )
    if not _has_grounding(candidate, results):
        return _outcome("retryable_validation_failure", ["ungrounded_answer"], revisions)
    citations = state.get("citations") or []
    if not citations:
        return _outcome("retryable_validation_failure", ["missing_citations"], revisions)
    return _outcome("validated", [], revisions)


def _validate_structured(state: GraphState, candidate: str, revisions: int) -> dict[str, object]:
    results = state.get("tool_results") or []
    if not results:
        return _outcome("invalid_result", ["no_tool_results"], revisions)
    successful = [result for result in results if result.get("status") == "success"]
    if not successful:
        last_error = (results[-1].get("error") or {}).get("code", "tool_error")
        return _outcome("invalid_result", [f"tool_error:{last_error}"], revisions)
    truncated = any((result.get("data") or {}).get("truncated") for result in successful)
    lowered = candidate.casefold()
    if truncated and "truncat" not in lowered:
        return _outcome("retryable_validation_failure", ["truncation_not_disclosed"], revisions)
    if not _mentions_result_evidence(candidate, successful):
        return _outcome("retryable_validation_failure", ["ungrounded_answer"], revisions)
    return _outcome("validated", [], revisions)


def _validate_general(state: GraphState, candidate: str, revisions: int) -> dict[str, object]:
    if len(candidate.split()) < 1:
        return _outcome("retryable_validation_failure", ["empty_general_answer"], revisions)
    return _outcome("validated", [], revisions)


def _outcome(status: ValidationStatus, reasons: list[str], revisions: int) -> dict[str, object]:
    return {
        "answer_validation": {
            "status": status,
            "reasons": reasons,
            "revision_count": revisions,
        }
    }


def _has_grounding(candidate: str, results: list[dict[str, object]]) -> bool:
    """知识类回答必须与其召回证据重叠，或引用某个来源标签。"""

    lowered = candidate.casefold()
    for item in results:
        label = str((item.get("metadata") or {}).get("label") or "").strip().casefold()
        if label and label in lowered:
            return True
        content = str(item.get("content") or "").casefold()
        for token in _significant_tokens(content)[:60]:
            if token in lowered:
                return True
    return False


def _mentions_result_evidence(candidate: str, results: list[dict[str, object]]) -> bool:
    lowered = candidate.casefold()
    for result in results:
        data = result.get("data") or {}
        row_count = data.get("row_count")
        if row_count is not None and str(row_count) in lowered:
            return True
        for token in _significant_tokens(str(data.get("query_summary") or ""))[:20]:
            if token in lowered:
                return True
    # 简短的事实性总结只要点出返回的列名，即视为合理使用了证据。
    for result in results:
        for column in (result.get("data") or {}).get("columns") or []:
            if str(column).casefold() in lowered:
                return True
    return False


def _significant_tokens(text: str) -> list[str]:
    import re

    return [token for token in re.findall(r"[a-z0-9_]{4,}", text.casefold())]


def should_revise(state: GraphState, budgets: ExecutionBudgets) -> bool:
    """仅当出现可重试的失败且仍在候选级预算内时，才允许 revision。"""

    validation = state.get("answer_validation") or {}
    if validation.get("status") != "retryable_validation_failure":
        return False
    if state.get("intent") not in REVISION_ELIGIBLE_INTENTS:
        return False
    revisions = int(state.get("answer_revision_count") or 0)
    return budgets.can_revise(revisions)


def revision_update(state: GraphState) -> dict[str, object]:
    """递增候选级的 revision 计数器并清空当前候选答案。"""

    return {
        "answer_revision_count": int(state.get("answer_revision_count") or 0) + 1,
        "answer_candidate": None,
        "answer_validation": None,
    }
