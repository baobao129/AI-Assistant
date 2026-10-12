from backend.domain.state import initial_state
from backend.graph.main import RAGNodes, response_finalize
from backend.graph.policies import ExecutionBudgets
from backend.graph.validation import GroundedAnswerValidator, validate_answer
from backend.infrastructure.config import Settings


def state(answer="采购审批金额上限为5000元。[采购规则]"):
    value = initial_state(
        conversation_id="c", thread_id="t", run_id="r", user_input="采购规则金额上限"
    )
    value.update(
        intent="knowledge",
        routing_outcome="routed",
        context="采购审批金额上限为5000元。",
        answer_candidate=answer,
        citations=[{"source_id": "d", "label": "采购规则"}],
        retrieval_results=[
            {
                "chunk_id": "ch",
                "content": "采购审批金额上限为5000元。",
                "metadata": {"label": "采购规则"},
                "citation": {"source_id": "d", "label": "采购规则"},
            }
        ],
    )
    return value


def test_label_alone_and_wrong_number_cannot_validate():
    for answer in ("采购规则：月球由奶酪制成。", "采购审批金额上限为9000元。[采购规则]"):
        assert (
            validate_answer(state(answer))["answer_validation"]["status"]
            == "retryable_validation_failure"
        )


def test_chinese_fact_support_passes_with_real_source():
    assert validate_answer(state())["answer_validation"]["status"] == "validated"


def test_exhausted_revision_is_safe_and_has_no_citations():
    value = state("无依据的答案")
    value.update(
        answer_revision_count=1, answer_validation={"status": "retryable_validation_failure"}
    )
    result = response_finalize(value)
    assert result["final_response"] != value["answer_candidate"]
    assert result["citations"] == []


def test_context_keeps_only_exact_selected_evidence():
    nodes = RAGNodes(
        tools=None,
        general_llm=None,
        settings=Settings(_env_file=None, max_context_chars=500, max_context_tokens=10000),
    )
    value = state()
    value["retrieval_results"][0]["content"] = "采购规则正文" * 500
    value["retrieval_results"].append(
        {
            "chunk_id": "unused",
            "content": "未进入证据",
            "metadata": {"label": "unused"},
            "citation": {"source_id": "other", "label": "unused"},
        }
    )
    result = nodes.context_build(value)
    assert len(result["context"]) <= 500
    assert len(result["retrieval_results"]) == 1
    assert result["retrieval_results"][0]["content"] in result["context"]
    assert result["citations"] == [{"source_id": "d", "label": "采购规则"}]


def test_semantic_judge_fails_closed_on_malformed_or_unknown_source():
    class Judge:
        def __init__(self, response):
            self.response = response

        def generate(self, prompt, value):
            return self.response

    for response in (
        "yes",
        '{"supported":true,"evidence_ids":["foreign"]}',
        '{"supported":false,"evidence_ids":["ch"]}',
    ):
        result = GroundedAnswerValidator(Judge(response))(state(), ExecutionBudgets())
        assert result["answer_validation"]["status"] == "retryable_validation_failure"
    result = GroundedAnswerValidator(Judge('{"supported":true,"evidence_ids":["ch"]}'))(
        state(), ExecutionBudgets()
    )
    assert result["answer_validation"]["status"] == "validated"


def test_semantic_judge_checks_translated_evidence_without_lexical_overlap():
    value = state("短途航班必须预订经济舱。[采购规则]")
    value["context"] = "Employees must book economy class for short flights."
    value["retrieval_results"][0]["content"] = value["context"]

    class Judge:
        called = False

        def generate(self, prompt, state):
            self.called = True
            assert "economy class" in prompt
            assert "经济舱" in prompt
            return '{"supported":true,"evidence_ids":["ch"]}'

    judge = Judge()
    result = GroundedAnswerValidator(judge)(value, ExecutionBudgets())
    assert judge.called
    assert result["answer_validation"]["status"] == "validated"


def test_semantic_judge_rejects_unsupported_translation():
    value = state("短途航班允许预订头等舱。[采购规则]")
    value["context"] = "Employees must book economy class for short flights."
    value["retrieval_results"][0]["content"] = value["context"]

    class Judge:
        def generate(self, prompt, state):
            return '{"supported":false,"evidence_ids":["ch"]}'

    result = GroundedAnswerValidator(Judge())(value, ExecutionBudgets())
    assert result["answer_validation"]["status"] == "retryable_validation_failure"


def test_semantic_judge_cannot_override_wrong_numbers_or_direct_negation():
    class Judge:
        def generate(self, prompt, state):
            raise AssertionError("deterministic contradiction must not reach the judge")

    for answer in (
        "采购审批金额上限为9000元。[采购规则]",
        "采购审批金额上限不能为5000元。[采购规则]",
    ):
        result = GroundedAnswerValidator(Judge())(state(answer), ExecutionBudgets())
        assert result["answer_validation"]["status"] == "retryable_validation_failure"


def test_unrelated_negative_sentence_does_not_block_supported_claim():
    value = state("不带思考能力的模型在面对复杂决策任务时表现往往很差。[采购规则]")
    value["context"] = (
        "模型选型不能只比较准确率、价格和速度，还要测试任务和接口能力。"
        "不带思考能力的模型在面对复杂决策任务时表现往往很差。"
    )
    value["retrieval_results"][0]["content"] = value["context"]

    class Judge:
        called = False

        def generate(self, prompt, state):
            self.called = True
            return '{"supported":true,"evidence_ids":["ch"]}'

    judge = Judge()
    result = GroundedAnswerValidator(judge)(value, ExecutionBudgets())
    assert judge.called
    assert result["answer_validation"]["status"] == "validated"
