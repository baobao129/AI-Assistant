"""`knowledge_search` 的跨 embedding 元数据兼容性测试（审计项 D2）。

背景：同一用户下可能同时存在不同 embedding provider/model/dimension 的 index version
（切换真实向量后必然出现：真实 BGE-M3 为 1024 维，历史离线验收语料为 64 维）。
查询向量只能检索 embedding 元数据兼容的 version；不兼容的 version 必须在执行任何
vector search **之前**被跳过，而不是靠捕获 Milvus 维度错误来掩盖问题。
全部候选版本都不兼容时才安全失败（`knowledge_index_incompatible`）。

因此本文件的断言既检查最终 Tool Result，也通过 RecordingVectorStore 检查
「不兼容的 index version / collection 从未真正触发向量检索」。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.application.index_registry import IndexRegistry
from backend.domain.identifiers import new_id, utc_now
from backend.infrastructure.config import Settings
from backend.infrastructure.repositories.documents import (
    ChunkRecord,
    DocumentRecord,
    IndexVersionRecord,
)
from backend.infrastructure.vector_store import VectorMatch, collection_name
from backend.tools.contracts import ToolExecutionContext, ToolResult
from backend.tools.data_tools import KnowledgeSearchInput, KnowledgeSearchTool

REAL_PROVIDER = "siliconflow"
REAL_MODEL = "BAAI/bge-m3"
REAL_DIMENSION = 1024
LEGACY_PROVIDER = "deterministic"
LEGACY_MODEL = "deterministic-hash"
LEGACY_DIMENSION = 64


class FakeEmbeddingProvider:
    """返回固定维度向量的离线 provider；元数据用于兼容性判定。"""

    def __init__(self, *, provider_name: str, model: str, dimension: int) -> None:
        self.provider_name = provider_name
        self.model = model
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        return [0.01] * self.dimension

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


def test_hybrid_keeps_sparse_only_candidates_and_excludes_foreign_content():
    versions = [
        _version(doc, provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION)
        for doc in ("a", "b")
    ]
    scenario = _scenario(versions=versions)

    class Scoped:
        def search_scope(self, *, sparse=False, **kwargs):
            assert set(kwargs["versions"]) == {"v-a", "v-b"}
            if sparse:
                return [_match("b", "v-b"), _match("foreign", "v-foreign")]
            return [_match("a", "v-a")]

    class Rerank:
        def rerank(self, query, documents, *, top_n):
            assert len(documents) == 2
            return [(1, 0.9), (0, 0.1)]

    scenario.tool.vectors = Scoped()
    scenario.tool.reranker = Rerank()
    result = scenario.search(top_k=2)
    assert result.status == "success"
    hits = result.data["results"]
    assert [hit["document_id"] for hit in hits] == ["b", "a"]
    assert hits[0]["scores"]["rrf_score"] == round(1 / 61, 6)
    assert "dense_score" not in hits[0]["scores"]
    assert hits[0]["retrieval_mode"] == "hybrid_rerank"


def test_rerank_failure_preserves_global_fusion_order():
    from backend.infrastructure.reranker import RerankerError

    versions = [
        _version(doc, provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION)
        for doc in ("a", "b")
    ]
    scenario = _scenario(versions=versions)

    class Scoped:
        def search_scope(self, *, sparse=False, **kwargs):
            return [_match("b", "v-b")] if sparse else [_match("a", "v-a"), _match("b", "v-b")]

    class Broken:
        def rerank(self, *args, **kwargs):
            raise RerankerError("unavailable")

    scenario.tool.vectors = Scoped()
    scenario.tool.reranker = Broken()
    hits = scenario.search(top_k=2).data["results"]
    assert [hit["document_id"] for hit in hits] == ["b", "a"]
    assert all(hit["rerank_degraded"] for hit in hits)


def test_dense_mode_never_calls_sparse_or_rerank_and_retains_cosine():
    versions = [_version("a", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION)]
    scenario = _scenario(versions=versions)
    scenario.tool.settings = Settings(_env_file=None, retrieval_mode="dense")

    class Scoped:
        def search_scope(self, *, sparse=False, **kwargs):
            assert not sparse
            return [_match("a", "v-a")]

    class Forbidden:
        def rerank(self, *args, **kwargs):
            raise AssertionError("disabled mode rerank")

    scenario.tool.vectors = Scoped()
    scenario.tool.reranker = Forbidden()
    hit = scenario.search().data["results"][0]
    assert hit["score"] == 0.92 and hit["retrieval_mode"] == "dense"


class RecordingVectorStore:
    """记录每次真正执行的向量检索，供断言不兼容版本没有被搜索。"""

    def __init__(self, matches: dict[str, list[VectorMatch]] | None = None) -> None:
        self.matches = matches or {}
        self.searches: list[dict[str, object]] = []

    def search(
        self,
        *,
        collection: str,
        embedding: list[float],
        top_k: int,
        document_ids: tuple[str, ...] = (),
        index_version: str | None = None,
    ) -> list[VectorMatch]:
        self.searches.append(
            {
                "collection": collection,
                "index_version": index_version,
                "dimension": len(embedding),
            }
        )
        return list(self.matches.get(index_version or "", []))


class FakeVersionRepository:
    def __init__(self, records: list[IndexVersionRecord]) -> None:
        self.records = {record.index_version: record for record in records}

    def get(self, index_version: str) -> IndexVersionRecord | None:
        return self.records.get(index_version)


class FakeDocumentRepository:
    """按 owner 过滤的 Document 仓储桩：未授权即视为不存在。"""

    def __init__(self, records: list[DocumentRecord]) -> None:
        self.records = {record.document_id: record for record in records}

    def get(self, document_id: str, *, user_id: str | None = None) -> DocumentRecord | None:
        record = self.records.get(document_id)
        if record is None:
            return None
        if user_id is not None and record.user_id != user_id:
            return None
        return record


class FakeChunkRepository:
    def __init__(self, records: list[ChunkRecord]) -> None:
        self.records = {record.chunk_id: record for record in records}

    def list_by_ids(self, chunk_ids: list[str], *, index_version: str | None) -> list[ChunkRecord]:
        return [self.records[chunk_id] for chunk_id in chunk_ids if chunk_id in self.records]


def _version(
    document_id: str,
    *,
    provider: str,
    model: str,
    dimension: int,
    status: str = "active",
) -> IndexVersionRecord:
    now = utc_now()
    return IndexVersionRecord(
        index_version=f"v-{document_id}",
        document_id=document_id,
        status=status,
        chunk_count=1,
        embedding_provider=provider,
        embedding_model=model,
        embedding_dimension=dimension,
        collection_name=collection_name(provider=provider, model=model, dimension=dimension),
        parser_provider="lightweight",
        failure_code=None,
        created_at=now,
        updated_at=now,
        retired_at=None,
    )


def _document(
    document_id: str,
    active_version: str | None,
    *,
    user_id: str = "user-1",
    status: str = "available",
) -> DocumentRecord:
    now = utc_now()
    return DocumentRecord(
        document_id=document_id,
        user_id=user_id,
        title=f"{document_id}.md",
        source_name=f"{document_id}.md",
        content_type="text/markdown",
        content_fingerprint="f" * 64,
        byte_size=10,
        status=status,
        active_index_version=active_version,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


def _chunk(document_id: str, index_version: str) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{index_version}-c0",
        document_id=document_id,
        index_version=index_version,
        parent_chunk_id=None,
        chunk_role="child",
        ordinal=0,
        content="economy class travel policy content",
        location="chunk-0",
        label="Travel Policy",
    )


def _match(document_id: str, index_version: str) -> VectorMatch:
    return VectorMatch(
        chunk_id=f"{index_version}-c0",
        document_id=document_id,
        index_version=index_version,
        parent_chunk_id=None,
        score=0.92,
    )


@dataclass
class Scenario:
    """一次检索场景的装配：真实 Tool + 记录型向量存储。"""

    tool: KnowledgeSearchTool
    vectors: RecordingVectorStore
    document_ids: tuple[str, ...]

    def search(self, query: str = "travel policy economy class", **kwargs) -> ToolResult:
        context = ToolExecutionContext(user_id="user-1", authorized_document_ids=self.document_ids)
        return self.tool.execute(
            f"call-{new_id()[:8]}", KnowledgeSearchInput(query=query, **kwargs), context
        )


def _scenario(
    *,
    query_provider: str = REAL_PROVIDER,
    query_model: str = REAL_MODEL,
    query_dimension: int = REAL_DIMENSION,
    versions: list[IndexVersionRecord] | None = None,
    matches: dict[str, list[VectorMatch]] | None = None,
    chunks: list[ChunkRecord] | None = None,
    documents: list[DocumentRecord] | None = None,
) -> Scenario:
    versions = versions or []
    if documents is None:
        documents = [_document(version.document_id, version.index_version) for version in versions]
    chunks = (
        chunks
        if chunks is not None
        else [_chunk(version.document_id, version.index_version) for version in versions]
    )
    vectors = RecordingVectorStore(matches)
    version_repository = FakeVersionRepository(versions)
    document_repository = FakeDocumentRepository(documents)
    chunk_repository = FakeChunkRepository(chunks)
    tool = KnowledgeSearchTool(
        settings=Settings(_env_file=None),
        embeddings=FakeEmbeddingProvider(
            provider_name=query_provider, model=query_model, dimension=query_dimension
        ),
        vectors=vectors,
        versions=version_repository,
        documents=document_repository,
        chunks=chunk_repository,
        # 使用真实 IndexRegistry：显式版本的 active/available/authorized 判定必须复用
        # application 层的既有逻辑，而不是测试自己的复制品。
        registry=IndexRegistry(
            documents=document_repository,
            versions=version_repository,
            chunks=chunk_repository,
        ),
    )
    return Scenario(
        tool=tool,
        vectors=vectors,
        document_ids=tuple(sorted(version.document_id for version in versions)),
    )


def test_single_compatible_version_searches_normally():
    # A. 单一兼容维度：正常执行检索并返回带引用的结果。
    version = _version(
        "doc-real",
        provider=REAL_PROVIDER,
        model=REAL_MODEL,
        dimension=REAL_DIMENSION,
    )
    scenario = _scenario(
        versions=[version],
        matches={version.index_version: [_match("doc-real", version.index_version)]},
    )

    result = scenario.search()

    assert result.status == "success"
    assert result.error is None
    assert len(result.data["results"]) == 1
    item = result.data["results"][0]
    assert item["citation"]["source_id"] == "doc-real"
    assert [(search["index_version"]) for search in scenario.vectors.searches] == [
        version.index_version
    ]
    assert scenario.vectors.searches[0]["collection"] == version.collection_name
    assert scenario.vectors.searches[0]["dimension"] == REAL_DIMENSION


def test_compatible_and_incompatible_versions_skip_only_incompatible():
    # B. 兼容版本与不兼容版本同时存在：只检索兼容版本，整体成功。
    legacy = _version(
        "doc-legacy",
        provider=LEGACY_PROVIDER,
        model=LEGACY_MODEL,
        dimension=LEGACY_DIMENSION,
    )
    real = _version(
        "doc-real",
        provider=REAL_PROVIDER,
        model=REAL_MODEL,
        dimension=REAL_DIMENSION,
    )
    scenario = _scenario(
        versions=[legacy, real],
        matches={real.index_version: [_match("doc-real", real.index_version)]},
    )

    result = scenario.search()

    assert result.status == "success"
    searched = [search["index_version"] for search in scenario.vectors.searches]
    assert searched == [real.index_version]
    assert legacy.index_version not in searched
    assert legacy.collection_name not in [
        search["collection"] for search in scenario.vectors.searches
    ]


def test_same_dimension_with_different_provider_or_model_is_skipped():
    # D. 混合 embedding provider/model/dimension：即使维度相同也跳过不匹配的版本。
    # 不同模型即使维度相同也不可比较；不同 provider 同样不保证同一向量空间。
    real = _version("doc-real", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION)
    foreign_provider = _version(
        "doc-foreign", provider="openai-compatible", model=REAL_MODEL, dimension=REAL_DIMENSION
    )
    foreign_model = _version(
        "doc-other-model",
        provider=REAL_PROVIDER,
        model="Qwen/Qwen3-Embedding-8B",
        dimension=REAL_DIMENSION,
    )
    legacy = _version(
        "doc-legacy",
        provider=LEGACY_PROVIDER,
        model=LEGACY_MODEL,
        dimension=LEGACY_DIMENSION,
    )
    scenario = _scenario(
        versions=[real, foreign_provider, foreign_model, legacy],
        matches={real.index_version: [_match("doc-real", real.index_version)]},
    )

    result = scenario.search()

    assert result.status == "success"
    searched = [search["index_version"] for search in scenario.vectors.searches]
    assert searched == [real.index_version]


def test_all_candidates_incompatible_returns_safe_failure_without_search():
    # C. 所有候选版本均不兼容：确定性安全失败，且没有触发任何检索。
    legacy = _version(
        "doc-legacy",
        provider=LEGACY_PROVIDER,
        model=LEGACY_MODEL,
        dimension=LEGACY_DIMENSION,
    )
    scenario = _scenario(versions=[legacy])

    result = scenario.search()

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "knowledge_index_incompatible"
    assert scenario.vectors.searches == []


def test_real_1024_and_historical_64_versions_coexist_without_overall_failure():
    # E. 真实 1024 维查询 + 历史 64 维 index 同时存在：全库检索不再整体失败。
    legacy = _version(
        "doc-legacy",
        provider=LEGACY_PROVIDER,
        model=LEGACY_MODEL,
        dimension=LEGACY_DIMENSION,
    )
    real = _version(
        "doc-real",
        provider=REAL_PROVIDER,
        model=REAL_MODEL,
        dimension=REAL_DIMENSION,
    )
    scenario = _scenario(
        versions=[legacy, real],
        matches={real.index_version: [_match("doc-real", real.index_version)]},
    )

    result = scenario.search()

    assert result.status == "success"
    assert len(result.data["results"]) == 1
    assert result.data["results"][0]["document_id"] == "doc-real"
    assert [search["dimension"] for search in scenario.vectors.searches] == [REAL_DIMENSION]


def test_explicit_incompatible_index_version_fails_safely():
    # 显式请求的 version 与查询向量不兼容：同样安全失败，不执行向量检索。
    legacy = _version(
        "doc-legacy",
        provider=LEGACY_PROVIDER,
        model=LEGACY_MODEL,
        dimension=LEGACY_DIMENSION,
    )
    scenario = _scenario(versions=[legacy])

    result = scenario.search(index_version=legacy.index_version)

    assert result.status == "failed"
    assert result.error is not None and result.error.code == "knowledge_index_incompatible"
    assert scenario.vectors.searches == []


def test_explicit_compatible_index_version_is_searched():
    # 显式请求兼容 version 时仍按该 version 限定检索（保持原有行为）。
    real = _version("doc-real", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION)
    scenario = _scenario(
        versions=[real],
        matches={real.index_version: [_match("doc-real", real.index_version)]},
    )

    result = scenario.search(index_version=real.index_version)

    assert result.status == "success"
    assert [search["index_version"] for search in scenario.vectors.searches] == [real.index_version]


def test_inactive_explicit_index_version_is_still_rejected():
    # 非 active 的显式 version 维持既有拒绝语义（不因兼容过滤而改变）。
    retired = _version(
        "doc-real",
        provider=REAL_PROVIDER,
        model=REAL_MODEL,
        dimension=REAL_DIMENSION,
        status="retired",
    )
    scenario = _scenario(versions=[retired])

    result = scenario.search(index_version=retired.index_version)

    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "index_version_not_active"
    assert scenario.vectors.searches == []


def test_documents_without_active_version_keep_empty_success():
    # 没有任何 active version 时维持既有语义：成功但无结果（不构成检索失败）。
    document = _document("doc-empty", None)
    vectors = RecordingVectorStore()
    versions = FakeVersionRepository([])
    documents = FakeDocumentRepository([document])
    chunks = FakeChunkRepository([])
    tool = KnowledgeSearchTool(
        settings=Settings(_env_file=None),
        embeddings=FakeEmbeddingProvider(
            provider_name=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION
        ),
        vectors=vectors,
        versions=versions,
        documents=documents,
        chunks=chunks,
        registry=IndexRegistry(documents=documents, versions=versions, chunks=chunks),
    )

    result = tool.execute(
        "call-empty",
        KnowledgeSearchInput(query="anything"),
        ToolExecutionContext(user_id="user-1", authorized_document_ids=("doc-empty",)),
    )

    assert result.status == "success"
    assert result.data["results"] == []
    assert vectors.searches == []


def test_explicit_version_of_another_user_is_rejected_before_search():
    # 越权：active + available 但属于其他用户的显式版本必须被拒绝，且绝不触发向量检索。
    version = _version(
        "doc-foreign", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION
    )
    scenario = _scenario(
        versions=[version],
        documents=[_document("doc-foreign", version.index_version, user_id="user-2")],
        matches={version.index_version: [_match("doc-foreign", version.index_version)]},
    )

    result = scenario.search(index_version=version.index_version)

    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "index_version_not_active"
    assert scenario.vectors.searches == []


def test_explicit_version_of_deleted_document_is_rejected_before_search():
    # 生命周期：active version 对应的 Document 已删除/不可用时必须拒绝，且不触发检索。
    version = _version(
        "doc-deleted", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION
    )
    scenario = _scenario(
        versions=[version],
        documents=[_document("doc-deleted", version.index_version, status="deleted")],
        matches={version.index_version: [_match("doc-deleted", version.index_version)]},
    )

    result = scenario.search(index_version=version.index_version)

    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "index_version_not_active"
    assert scenario.vectors.searches == []


def test_unknown_explicit_version_is_rejected_before_search():
    # 未知版本与未授权版本返回同一个安全拒绝码，不泄漏版本是否存在。
    scenario = _scenario()

    result = scenario.search(index_version="v-does-not-exist")

    assert result.status == "rejected"
    assert result.error is not None and result.error.code == "index_version_not_active"
    assert scenario.vectors.searches == []


def test_explicit_compatible_version_of_own_document_still_requires_authorization_scope():
    # 显式版本即使生命周期合法，也必须属于当前用户；authorized_document_ids 之外的其他用户
    # 文档不会被检索。
    version = _version(
        "doc-mine", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION
    )
    other = _version(
        "doc-other", provider=REAL_PROVIDER, model=REAL_MODEL, dimension=REAL_DIMENSION
    )
    scenario = _scenario(
        versions=[version, other],
        documents=[
            _document("doc-mine", version.index_version),
            _document("doc-other", other.index_version, user_id="user-2"),
        ],
        matches={other.index_version: [_match("doc-other", other.index_version)]},
    )

    result = scenario.search(index_version=other.index_version)

    assert result.status == "rejected"
    assert scenario.vectors.searches == []
