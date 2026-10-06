from backend.domain.state import GraphState


def validate_answer(state: GraphState) -> dict[str, object]:
    candidate = state.get("answer_candidate")
    intent = state.get("intent")
    if not candidate:
        return {"answer_validation": {"status": "invalid_result", "reasons": ["empty_answer"]}}
    if intent == "knowledge" and not state.get("retrieval_results"):
        return {
            "answer_validation": {
                "status": "insufficient_evidence",
                "reasons": ["no_authorized_evidence"],
            }
        }
    return {"answer_validation": {"status": "validated", "reasons": []}}
