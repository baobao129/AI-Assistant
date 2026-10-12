"""文档作用域（ADR-039 / CC-API-002 / CC-STATE-004 / CC-TOOL-003 / CC-GRAPH-003）单测。

覆盖：请求字段格式校验、A′ 六种路由组合、State 的写入与 Resume 保留、以及
``knowledge_search`` 在作用域不可用时的明确拒绝与**零 provider 调用**。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from backend.api.schemas import RunCreateRequest
from backend.domain.state import (
    MAX_DOCUMENT_SCOPE_IDS,
    bounded_document_scope,
    initial_state,
)
from backend.graph.main import IntentRouter, request_initialize
from backend.infrastructure.config import Settings
from backend.infrastructure.repositories.documents import DocumentRecord
from backend.tools.contracts import ToolExecutionContext
from backend.tools.data_tools import KnowledgeSearchTool

NOW = datetime(2026, 10, 9, 12, 0, 0)


def test_scoped_chinese_policy_question_routes_to_knowledge_without_changing_unscoped():
    question = "差旅报销制度规定的住宿费标准和审批要求是什么？"
    router = IntentRouter()
    assert router.route(question, document_scope=["d"])["intent"] == "knowledge"
    assert router.route(question)["intent"] == "general"
    assert router.route("你好", document_scope=["d"])["intent"] == "general"


# --- 请求字段格式校验（CC-API-002）------------------------------------------


def test_absent_and_empty_document_ids_both_mean_no_scope():
    assert RunCreateRequest(user_input="q").document_ids is None
    assert RunCreateRequest(user_input="q", document_ids=[]).document_ids == []


def test_document_ids_are_deduplicated_preserving_first_occurrence_order():
    request = RunCreateRequest(user_input="q", document_ids=["b", "a", "b", "c", "a"])
    assert request.document_ids == ["b", "a", "c"]


def test_duplicates_do_not_consume_the_limit():
    # 30 项里只有 20 个唯一值：去重后正好等于上限，必须通过。
    duplicated = [f"doc{index % MAX_DOCUMENT_SCOPE_IDS}" for index in range(30)]
    request = RunCreateRequest(user_input="q", document_ids=duplicated)
    assert len(request.document_ids) == MAX_DOCUMENT_SCOPE_IDS


def test_more_than_the_limit_unique_ids_is_rejected():
    with pytest.raises(ValidationError):
        RunCreateRequest(
            user_input="q",
            document_ids=[f"doc{index}" for index in range(MAX_DOCUMENT_SCOPE_IDS + 1)],
        )


def test_over_long_identifier_is_rejected():
    with pytest.raises(ValidationError):
        RunCreateRequest(user_input="q", document_ids=["d" * 65])


def test_empty_and_non_string_identifiers_are_rejected():
    with pytest.raises(ValidationError):
        RunCreateRequest(user_input="q", document_ids=[""])
    with pytest.raises(ValidationError):
        RunCreateRequest(user_input="q", document_ids=[123])


@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd",
        "/var/data/report.pdf",
        "https://example.com/a.pdf",
        "report.pdf",
        "a b",
        "doc?limit=1",
        "doc&x=1",
        "doc=1",
        "doc:1",
        "文档",
    ],
)
def test_paths_urls_filenames_and_query_conditions_are_rejected(value: str):
    # 作用域只接受不透明标识符，不能承载路径、URL、文件名或任意查询条件。
    with pytest.raises(ValidationError):
        RunCreateRequest(user_input="q", document_ids=[value])


def test_surrounding_whitespace_is_normalised_and_cannot_smuggle_separators():
    assert RunCreateRequest(user_input="q", document_ids=[" doc1 "]).document_ids == ["doc1"]
    with pytest.raises(ValidationError):
        RunCreateRequest(user_input="q", document_ids=[" a/b "])


# --- State（CC-STATE-004）---------------------------------------------------


def test_bounded_document_scope_deduplicates_and_truncates():
    assert bounded_document_scope(None) == []
    assert bounded_document_scope([]) == []
    assert bounded_document_scope(["a", "b", "a"]) == ["a", "b"]
    assert len(bounded_document_scope([f"d{i}" for i in range(50)])) == MAX_DOCUMENT_SCOPE_IDS


def test_initial_state_stores_the_validated_scope():
    state = initial_state(
        conversation_id="c1",
        thread_id="t1",
        run_id="r1",
        user_input="q",
        document_scope=["doc1", "doc2"],
    )
    assert state["document_scope"] == ["doc1", "doc2"]


def test_initial_state_defaults_to_an_empty_scope():
    state = initial_state(conversation_id="c1", thread_id="t1", run_id="r1", user_input="q")
    assert state["document_scope"] == []


def test_request_initialize_must_not_reset_the_document_scope():
    # 这是本功能最隐蔽的回归点：Resume 与审批 successor 依赖 Checkpoint 恢复作用域，
    # request_initialize 一旦清空它，续跑会静默把检索范围放回「全部授权文档」。
    update = request_initialize(
        {
            "document_scope": ["doc1"],
            "answer_revision_count": 0,
            "error": None,
        }
    )
    assert "document_scope" not in update


# --- A′ 路由（CC-GRAPH-003）------------------------------------------------


def _route(query: str, scope: tuple[str, ...] = ()):
    outcome = IntentRouter().route(query, document_scope=scope)
    return outcome["intent"], outcome["routing_outcome"], outcome["current_path"]


KNOWLEDGE_QUERY = "what does the policy manual say"
STRUCTURED_QUERY = "how much is the sales amount"
MIXED_QUERY = "what does the policy manual say about the sales amount"
GREETING_QUERY = "hello there"


def test_without_scope_knowledge_query_is_unchanged():
    assert _route(KNOWLEDGE_QUERY) == ("knowledge", "routed", "knowledge")


def test_without_scope_structured_query_is_unchanged():
    assert _route(STRUCTURED_QUERY) == ("structured_data", "routed", "structured_data")


def test_without_scope_mixed_query_still_refuses():
    assert _route(MIXED_QUERY) == (None, "unsupported_mixed_query", None)


def test_with_scope_a_structured_query_is_taken_by_knowledge():
    # 验收核心：选中文档后，金额/数量这类词不能把请求抢到 SQL 路径。
    assert _route(STRUCTURED_QUERY, ("doc1",)) == ("knowledge", "routed", "knowledge")


def test_with_scope_a_mixed_query_resolves_to_knowledge():
    assert _route(MIXED_QUERY, ("doc1",)) == ("knowledge", "routed", "knowledge")


def test_with_scope_a_greeting_still_goes_to_general():
    assert _route(GREETING_QUERY, ("doc1",)) == ("general", "routed", "general")


def test_with_scope_knowledge_query_stays_knowledge():
    assert _route(KNOWLEDGE_QUERY, ("doc1",)) == ("knowledge", "routed", "knowledge")


def test_empty_query_and_routing_error_keep_their_outcomes_with_a_scope():
    assert _route("   ", ("doc1",)) == (None, "invalid_request", None)
    assert _route("__routing_error__", ("doc1",)) == (None, "routing_error", None)


# --- 检索边界（CC-TOOL-003）------------------------------------------------


class _RecordingEmbeddings:
    provider_name = "deterministic"
    model = "test-model"
    dimension = 4

    def __init__(self) -> None:
        self.embed_calls = 0

    def embed(self, text: str) -> list[float]:
        self.embed_calls += 1
        return [0.1, 0.2, 0.3, 0.4]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _RecordingVectors:
    def __init__(self) -> None:
        self.search_calls = 0
        self.searched_versions: list[str] = []

    def search(self, **kwargs):
        self.search_calls += 1
        self.searched_versions.append(str(kwargs.get("index_version")))
        return []


class _Documents:
    def __init__(self, records: dict[str, DocumentRecord]) -> None:
        self.records = records

    def get(self, document_id: str, *, user_id: str | None = None):
        return self.records.get(document_id)


class _Versions:
    def __init__(self, records: dict[str, object]) -> None:
        self.records = records

    def get(self, index_version: str):
        return self.records.get(index_version)


def _document(document_id: str, *, status: str = "available", active: str | None = "v1"):
    return DocumentRecord(
        document_id=document_id,
        user_id="u1",
        title=document_id,
        source_name=f"{document_id}.pdf",
        content_type="application/pdf",
        content_fingerprint="f" * 64,
        byte_size=1024,
        status=status,
        active_index_version=active,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )


def _version(index_version: str, *, document_id: str = "mine"):
    from backend.infrastructure.repositories.documents import IndexVersionRecord

    return IndexVersionRecord(
        index_version=index_version,
        document_id=document_id,
        status="active",
        chunk_count=1,
        embedding_provider="deterministic",
        embedding_model="test-model",
        embedding_dimension=4,
        collection_name="knowledge_deterministic_test_model_4",
        parser_provider="lightweight",
        failure_code=None,
        created_at=NOW,
        updated_at=NOW,
        retired_at=None,
    )


def _tool(
    *,
    documents: dict[str, DocumentRecord],
    authorized: tuple[str, ...],
    versions: dict[str, object] | None = None,
):
    embeddings = _RecordingEmbeddings()
    vectors = _RecordingVectors()
    tool = KnowledgeSearchTool(
        settings=Settings(_env_file=None),
        embeddings=embeddings,
        vectors=vectors,
        versions=_Versions(versions or {}),
        documents=_Documents(documents),
        chunks=None,
        registry=None,
    )
    context = ToolExecutionContext(user_id="u1", authorized_document_ids=authorized)
    return tool, embeddings, vectors, context


def test_a_scope_outside_the_authorized_set_is_rejected_without_any_provider_call():
    tool, embeddings, vectors, context = _tool(
        documents={"mine": _document("mine")}, authorized=("mine",)
    )
    result = tool.execute(
        "call-1",
        tool.input_model(query="anything", document_ids=["someone-elses"]),
        context,
    )
    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "document_scope_unavailable"
    assert embeddings.embed_calls == 0
    assert vectors.search_calls == 0


def test_a_deleted_selected_document_is_rejected_like_a_foreign_one():
    tool, embeddings, vectors, context = _tool(
        documents={"gone": _document("gone", status="deleted")}, authorized=("gone",)
    )
    result = tool.execute(
        "call-1", tool.input_model(query="anything", document_ids=["gone"]), context
    )
    # 同一个码与同一段文案：不泄漏「已删除」与「不属于你」的区别。
    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "document_scope_unavailable"
    assert embeddings.embed_calls == 0 and vectors.search_calls == 0


def test_a_selected_document_without_an_active_version_is_rejected():
    tool, embeddings, vectors, context = _tool(
        documents={"empty": _document("empty", active=None)}, authorized=("empty",)
    )
    result = tool.execute(
        "call-1", tool.input_model(query="anything", document_ids=["empty"]), context
    )
    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "document_scope_unavailable"
    assert embeddings.embed_calls == 0 and vectors.search_calls == 0


def test_no_scope_without_authorized_documents_keeps_the_previous_behavior():
    # 无作用域必须逐行保持原行为：空结果的成功应答，而不是拒绝。
    tool, embeddings, vectors, context = _tool(documents={}, authorized=())
    result = tool.execute("call-1", tool.input_model(query="anything"), context)
    assert result.status == "success"
    assert result.data is not None and result.data["results"] == []
    assert embeddings.embed_calls == 0 and vectors.search_calls == 0


def test_scope_can_only_narrow_never_widen():
    # 作用域里混入一个未授权 ID：交集把它丢掉，检索只发生在授权文档上。
    # （未授权 ID 的**拒绝**发生在建 Run 的快速校验，这里验证的是边界语义：
    # 交集只可能收窄，绝不会因为请求里写了别人的 ID 而检索到别人的内容。）
    tool, _embeddings, vectors, context = _tool(
        documents={"mine": _document("mine")},
        authorized=("mine",),
        versions={"v1": _version("v1")},
    )
    result = tool.execute(
        "call-1",
        tool.input_model(query="anything", document_ids=["mine", "theirs"]),
        context,
    )
    assert result.status == "success"
    assert vectors.searched_versions == ["v1"]
    assert result.data is not None and result.data["results"] == []
