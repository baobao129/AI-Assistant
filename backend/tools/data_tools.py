"""Contract Tool 实现：knowledge_search、database_schema、database_query、data_export。

每个 Tool 接收显式输入并返回规范化的 Tool Result。Tool 从不读取或修改
Graph State、Conversation Persistence 或 Checkpoint。
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.application.index_registry import IndexRegistry, IndexRegistryError
from backend.infrastructure.config import Settings
from backend.infrastructure.database import DatabaseUnavailableError, ReadOnlyQueryRunner
from backend.infrastructure.embedding import EmbeddingProvider, EmbeddingProviderError
from backend.infrastructure.repositories.approvals import ApprovalRepository
from backend.infrastructure.repositories.documents import (
    ChunkRecord,
    ChunkRepository,
    DocumentRepository,
    IndexVersionRecord,
    IndexVersionRepository,
)
from backend.infrastructure.repositories.structured import (
    ExportArtifactRepository,
    QueryResultRepository,
)
from backend.infrastructure.reranker import Reranker, RerankerError
from backend.infrastructure.vector_store import VectorStore, VectorStoreError, collection_name
from backend.tools.contracts import (
    RegisteredTool,
    ToolError,
    ToolExecutionContext,
    ToolMetadata,
    ToolResult,
)
from backend.tools.sql_validator import ProjectSQLValidator, SQLValidator

logger = logging.getLogger(__name__)


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    index_version: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)


class DatabaseSchemaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_source_id: str | None = None
    table_names: list[str] = Field(default_factory=list, max_length=20)


class DatabaseQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_source_id: str | None = None
    sql: str = Field(min_length=1, max_length=5000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    max_rows: int = Field(default=100, ge=1, le=1000)


class DataExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="database_query_result", max_length=40)
    query_result_id: str = Field(min_length=1, max_length=64)
    format: str = Field(default="csv", max_length=10)
    columns: list[str] = Field(default_factory=list, max_length=200)
    filters: dict[str, str] = Field(default_factory=dict)
    destination: str = Field(default="managed_export_area", max_length=40)
    reason: str = Field(min_length=1, max_length=500)


class KnowledgeSearchTool(RegisteredTool):
    """在授权 Child 上融合 Dense/BM25 并重排，Parent 映射交由 Graph 处理。"""

    metadata = ToolMetadata("knowledge_search", "1", "low", "knowledge_retrieval")
    input_model = KnowledgeSearchInput

    # 有界的检索窗口：`knowledge_search` 永远不会是一次无界扫描。
    RETRIEVAL_CANDIDATE_FACTOR = 4
    MAX_RETRIEVAL_CANDIDATES = 200
    MAX_DOCUMENT_SCOPE = 50

    def __init__(
        self,
        *,
        settings: Settings,
        embeddings: EmbeddingProvider,
        vectors: VectorStore,
        versions: IndexVersionRepository,
        documents: DocumentRepository,
        chunks: ChunkRepository,
        registry: IndexRegistry,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings
        self.embeddings = embeddings
        self.vectors = vectors
        self.versions = versions
        self.documents = documents
        self.chunks = chunks
        # 显式 index_version 的生命周期与授权判定复用 application 层的 Index Registry，
        # 不复制第二套版本授权逻辑。
        self.registry = registry
        self.reranker = reranker

    def execute(
        self,
        tool_call_id: str,
        arguments: KnowledgeSearchInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        top_k = min(arguments.top_k, self.settings.max_retrieval_top_k)
        documents = self._authorized_documents(arguments, context)
        # 文档作用域（ADR-039 / CC-TOOL-003）：请求了非空作用域、但当前可用授权集合与它
        # 的交集为空时，返回**明确的拒绝**而不是空成功结果 —— 后者会被读成「文档里没有
        # 答案」。这一步在任何 provider 调用（embedding / vector store）之前，
        # 因此拒绝路径不会产生任何 Dense、BM25、Milvus、Reranker 或 LLM 调用。
        scope_requested = bool(arguments.document_ids)
        if scope_requested and not documents:
            return self._rejected(
                tool_call_id,
                "document_scope_unavailable",
                "One or more selected documents are not available for this request.",
            )

        # 显式版本的生命周期与授权判定先于任何检索执行（也先于任何早期空结果返回）：
        # active + available + authorized 三者缺一都会被拒绝，绝不通过「先查询再过滤结果」
        # 实现授权。未找到、非 active、文档不可用与未授权统一映射为同一个安全拒绝码，
        # 不泄漏其他用户是否拥有该版本。
        explicit_version = None
        if arguments.index_version:
            user_id = context.user_id if context and context.user_id else ""
            try:
                resolved_version = self.registry.resolve_explicit_version(
                    arguments.index_version, user_id=user_id
                )
            except IndexRegistryError:
                return self._rejected(
                    tool_call_id, "index_version_not_active", "That index version is not active."
                )
            explicit_version = self.versions.get(resolved_version)
            if explicit_version is None:  # pragma: no cover - registry 已解析出该版本
                return self._rejected(
                    tool_call_id, "index_version_not_active", "That index version is not active."
                )
            if explicit_version.document_id not in {document.document_id for document in documents}:
                return self._rejected(
                    tool_call_id,
                    "document_scope_unavailable",
                    "One or more selected documents are not available for this request.",
                )
        elif not documents:
            return self._success(tool_call_id, arguments, [], started, truncated=False)

        # 候选版本在 **embedding 之前**计算：这样「作用域里的文档没有任何可用 active
        # 版本」能在调用任何 provider 之前被识别并明确拒绝（ADR-039 决策 8），
        # 而不是先花一次 embedding 再返回空结果。
        if explicit_version is not None:
            candidates = [explicit_version]
        else:
            # 每个已授权 Document 都通过自身的 active 版本分别检索，使查询保持有界，
            # 并避免使用一个宽泛的 `document_id IN (...)` 过滤条件。
            candidates = []
            for document in documents[: self.MAX_DOCUMENT_SCOPE]:
                if not document.active_index_version:
                    continue
                version = self.versions.get(document.active_index_version)
                if version is None or version.status != "active":
                    continue
                candidates.append(version)
            if not candidates:
                if scope_requested:
                    # 选中的文档没有任何可用的 active 版本：明确拒绝，绝不返回空成功
                    # 结果（那会被读成「选中的文档里没有答案」）。
                    return self._rejected(
                        tool_call_id,
                        "document_scope_unavailable",
                        "One or more selected documents are not available for this request.",
                    )
                return self._success(tool_call_id, arguments, [], started, truncated=False)

        try:
            embedding = self.embeddings.embed(arguments.query)
        except EmbeddingProviderError as error:
            return self._failed(
                tool_call_id, str(error), "Knowledge retrieval is temporarily unavailable."
            )
        default_collection = self._collection_for(embedding)
        # 查询向量的 embedding 元信息是兼容性判定的基准：provider/model/dimension 三者
        # 全部一致的 index version 才允许被该查询向量检索（PROJECT_SPEC §8/§9：模型或维度
        # 变化会生成新的集合/版本，历史版本仍保留在库中，不能因此让整次检索失败）。
        query_meta = (self.embeddings.provider_name, self.embeddings.model, len(embedding))

        # 兼容过滤发生在任何 vector search 之前：不兼容的 index version 直接跳过，
        # 绝不拿查询向量去搜索元数据不匹配的集合（那会触发向量维度错误并让整次检索失败）。
        targets: list[tuple[str, str]] = []
        skipped = 0
        for version in candidates:
            if not self._query_compatible(version, query_meta):
                skipped += 1
                continue
            targets.append((version.index_version, version.collection_name or default_collection))
        if not targets:
            # 全部候选版本都不兼容：这是确定性的索引能力问题，不是瞬时 provider 故障。
            # 既不检索不匹配的集合，也不允许 Graph 进入 repair 重试或无证据生成。
            logger.warning("knowledge_search_no_compatible_version skipped=%s", skipped)
            return self._failed(
                tool_call_id,
                "knowledge_index_incompatible",
                "The authorized knowledge index is incompatible with the configured "
                "embedding model.",
            )
        if skipped:
            # 有界日志：只记录被跳过的数量，不记录内容或标识符。
            logger.info("knowledge_search_skipped_incompatible_versions skipped=%s", skipped)

        groups = []
        sparse_groups = []
        collections: dict[str, list[str]] = {}
        for version_id, collection in targets:
            collections.setdefault(collection, []).append(version_id)
        scope_search = getattr(self.vectors, "search_scope", None)
        hybrid_collections = [name for name in collections if name.endswith("_hybrid_v1")]
        wants_sparse = self.settings.retrieval_mode in {"dense_bm25", "hybrid_rerank"}
        wants_rerank = self.settings.rerank_enabled and self.settings.retrieval_mode in {
            "dense_rerank",
            "hybrid_rerank",
        }
        sparse_available = wants_sparse and callable(scope_search) and len(hybrid_collections) == 1
        window = min(
            max(top_k, self.settings.rerank_top_n) * self.RETRIEVAL_CANDIDATE_FACTOR,
            self.MAX_RETRIEVAL_CANDIDATES,
        )
        for collection, version_ids in collections.items():
            try:
                if callable(scope_search):
                    groups.append(
                        scope_search(
                            collection=collection,
                            embedding=embedding,
                            query=arguments.query,
                            versions=tuple(version_ids),
                            top_k=window,
                        )
                    )
                else:
                    for version_id in version_ids:
                        groups.append(
                            self._search(
                                collection=collection,
                                embedding=embedding,
                                top_k=top_k,
                                index_version=version_id,
                            )
                        )
                if sparse_available and collection in hybrid_collections:
                    sparse_groups.append(
                        scope_search(
                            collection=collection,
                            query=arguments.query,
                            embedding=embedding,
                            versions=tuple(version_ids),
                            top_k=window,
                            sparse=True,
                        )
                    )
            except VectorStoreError as error:
                return self._failed(
                    tool_call_id, str(error), "Knowledge retrieval is temporarily unavailable."
                )

        allowed_ids = {document.document_id for document in documents}
        dense_ranked = sorted(
            [match for group in groups for match in group],
            key=lambda match: (-match.score, match.chunk_id),
        )[:window]
        sparse_ranked = [match for group in sparse_groups for match in group]
        target_ids = {version_id for version_id, _ in targets}
        allowed_pairs = {
            (version.document_id, version.index_version)
            for version in candidates
            if version.index_version in target_ids
        }
        merged: dict[str, dict[str, float]] = {}
        for rank, match in enumerate(dense_ranked, start=1):
            if (match.document_id, match.index_version) not in allowed_pairs:
                continue
            item = merged.setdefault(match.chunk_id, {})
            item["dense_score"] = max(item.get("dense_score", match.score), match.score)
            item["dense_rank"] = min(item.get("dense_rank", float(rank)), float(rank))
        for rank, match in enumerate(sparse_ranked, start=1):
            if (match.document_id, match.index_version) not in allowed_pairs:
                continue
            item = merged.setdefault(match.chunk_id, {})
            item["bm25_score"] = max(item.get("bm25_score", match.score), match.score)
            item["bm25_rank"] = min(item.get("bm25_rank", float(rank)), float(rank))
        if not merged:
            return self._success(tool_call_id, arguments, [], started, truncated=False)
        for scores in merged.values():
            scores["rrf_score"] = (
                1.0 / (60.0 + scores["dense_rank"]) if "dense_rank" in scores else 0.0
            ) + (1.0 / (60.0 + scores["bm25_rank"]) if "bm25_rank" in scores else 0.0)
        ranked = sorted(merged.items(), key=lambda item: (-item[1]["rrf_score"], item[0]))

        chunks = self.chunks.list_by_ids([chunk_id for chunk_id, _ in ranked], index_version=None)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        results: list[dict[str, Any]] = []
        for chunk_id, scores in ranked:
            chunk = by_id.get(chunk_id)
            if (
                chunk is None
                or chunk.document_id not in allowed_ids
                or (chunk.document_id, chunk.index_version) not in allowed_pairs
            ):
                continue
            document = self.documents.get(chunk.document_id)
            if (
                document is None
                or document.status != "available"
                or document.active_index_version != chunk.index_version
            ):
                if scope_requested:
                    return self._rejected(
                        tool_call_id,
                        "document_scope_unavailable",
                        "One or more selected documents are not available for this request.",
                    )
                continue
            score = scores["rrf_score"] if sparse_available else scores.get("dense_score", 0.0)
            results.append(self._result_item(chunk, score, scores=scores))
            if len(results) >= min(window, max(top_k, self.settings.rerank_top_n)):
                break
        truncated = len(results) > top_k or len(ranked) > len(results)
        rerank_scores: dict[str, float] = {}
        rerank_degraded = False
        if results and wants_rerank and self.reranker is not None:
            try:
                texts: list[str] = []
                remaining = self.settings.rerank_max_total_chars
                for item in results:
                    text = str(item["content"])[
                        : min(remaining, self.settings.rerank_max_document_chars)
                    ]
                    if not text:
                        break
                    texts.append(text)
                    remaining -= len(text)
                reranked = self.reranker.rerank(
                    arguments.query,
                    texts,
                    top_n=min(self.settings.rerank_top_n, len(texts)),
                )
                rerank_scores = {results[index]["chunk_id"]: score for index, score in reranked}
                ordered = [results[index] for index, _ in reranked]
                results = ordered + [item for item in results if item not in ordered]
            except RerankerError:
                rerank_degraded = True
        for item in results:
            if item["chunk_id"] in rerank_scores:
                item["rerank_score"] = rerank_scores[item["chunk_id"]]
            item["retrieval_mode"] = (
                ("hybrid_rerank" if sparse_available else "dense_rerank")
                if rerank_scores
                else "hybrid"
                if sparse_available
                else "dense"
            )
            item["rerank_degraded"] = rerank_degraded
            item["sparse_degraded"] = wants_sparse and not sparse_available
            if item["chunk_id"] in rerank_scores:
                item["scores"]["rerank_score"] = rerank_scores[item["chunk_id"]]
        return self._success(tool_call_id, arguments, results[:top_k], started, truncated=truncated)

    def _collection_for(self, embedding: list[float]) -> str:
        return collection_name(
            provider=self.embeddings.provider_name,
            model=self.embeddings.model,
            dimension=len(embedding),
        )

    def _query_compatible(
        self, version: IndexVersionRecord, query_meta: tuple[str, str, int]
    ) -> bool:
        """查询向量只能检索 embedding 元数据一致的 index version。

        三个条件都必须满足：provider/model 不同的向量空间不可比较（即使维度相同），
        维度不同则 Milvus 会直接拒绝。判定只使用 ``document_index_versions`` 已登记的
        字段，不新增架构字段，也不依赖捕获 Milvus 异常。
        """

        provider, model, dimension = query_meta
        return (
            version.embedding_provider == provider
            and version.embedding_model == model
            and version.embedding_dimension == dimension
        )

    def _search(self, *, collection: str, embedding: list[float], top_k: int, index_version: str):
        """按 index version 限定范围的有界向量检索。

        不向 Milvus 请求大型 ``document_id IN (...)`` 表达式：该复合过滤在本运行时
        可能召回不足，因此改为按 index version 限定查询，并在返回行上强制授权。
        候选窗口保持有界。
        """

        window = max(top_k, self.settings.max_retrieval_top_k) * self.RETRIEVAL_CANDIDATE_FACTOR
        return self.vectors.search(
            collection=collection,
            embedding=embedding,
            top_k=min(window, self.MAX_RETRIEVAL_CANDIDATES),
            index_version=index_version,
        )

    def _authorized_documents(
        self, arguments: KnowledgeSearchInput, context: ToolExecutionContext | None
    ):
        """在服务端授权范围内解析候选 Document。

        调用方的作用域是权威来源。请求的 `document_ids` 只能将其收窄；
        对范围之外的 Document 请求将不返回任何证据。
        """

        scope = set(context.authorized_document_ids) if context else set()
        requested = set(arguments.document_ids)
        candidates = scope & requested if requested else set(scope)
        allowed: list[Any] = []
        for document_id in sorted(candidates):
            document = self.documents.get(document_id)
            if document is not None and document.status == "available":
                allowed.append(document)
        return allowed

    def _result_item(
        self, chunk: ChunkRecord, score: float, *, scores: dict[str, float] | None = None
    ) -> dict[str, Any]:
        # 结构元数据随结果一起返回（CC-TOOL-002）：调用方可以据此给出真实页码与章节，
        # 而不是把 chunk 序号当定位。没有可靠来源时字段为 None，绝不伪造。
        return {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "parent_chunk_id": chunk.parent_chunk_id,
            "index_version": chunk.index_version,
            "content": chunk.content,
            "score": round(float(score), 6),
            "scores": {key: round(float(value), 6) for key, value in (scores or {}).items()},
            "section_path": chunk.section_path,
            "block_type": chunk.block_type,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "token_count": chunk.token_count,
            "metadata": {
                "label": chunk.label,
                "location": chunk.location,
                "section_path": chunk.section_path,
                "block_type": chunk.block_type,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
            },
            "citation": {
                "source_id": chunk.document_id,
                "location": chunk.location,
                "label": chunk.label,
                "section_path": chunk.section_path,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
            },
        }

    def _success(
        self,
        tool_call_id: str,
        arguments: KnowledgeSearchInput,
        results: list[dict[str, Any]],
        started: float,
        *,
        truncated: bool,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={"query": arguments.query, "results": results, "truncated": truncated},
            metadata={
                "duration_ms": int((time.monotonic() - started) * 1000),
                "truncated": truncated,
            },
        )

    def _rejected(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="rejected",
            error=ToolError(code=code, message=message),
        )

    def _failed(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="failed",
            error=ToolError(code=code[:64], message=message),
        )


class DatabaseSchemaTool(RegisteredTool):
    """从 read-only 账号进行白名单 schema 发现；不访问系统目录。"""

    metadata = ToolMetadata("database_schema", "1", "low", "structured_schema")
    input_model = DatabaseSchemaInput

    def __init__(self, *, settings: Settings, query_runner: ReadOnlyQueryRunner) -> None:
        self.settings = settings
        self.query_runner = query_runner

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseSchemaInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        requested = [name.casefold() for name in arguments.table_names]
        allowed = [
            table
            for table in self.settings.allowed_table_names
            if not requested or table.casefold() in requested
        ]
        if requested and not allowed:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(
                    code="table_not_allowed", message="The requested table is not available."
                ),
            )
        if not allowed:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="success",
                data={
                    "data_source_id": arguments.data_source_id or self.settings.data_source_id,
                    "schema_version": self.settings.schema_version,
                    "tables": [],
                },
            )
        placeholders = ", ".join(["%s"] * len(allowed))
        outcome = self.query_runner.run(
            "SELECT table_name, column_name, data_type, is_nullable, ordinal_position "
            "FROM information_schema.columns "
            f"WHERE table_schema = %s AND table_name IN ({placeholders}) "
            "ORDER BY table_name, ordinal_position",
            (self.settings.mysql_database, *allowed),
            max_rows=500,
        )
        if outcome.status != "success":
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code=outcome.error_code or "database_error",
                    message="Schema metadata is temporarily unavailable.",
                ),
            )
        tables: dict[str, dict[str, Any]] = {
            name: {"table_name": name, "description": None, "columns": []} for name in allowed
        }
        for row in outcome.rows:
            table_name = str(row[0])
            table = tables.get(table_name)
            if table is None:
                continue
            column_name = str(row[1])
            table["columns"].append(
                {
                    "name": column_name,
                    "type": str(row[2]),
                    "nullable": str(row[3]).upper() == "YES",
                    "description": None,
                    "masked": column_name.casefold() in self.settings.masked_column_names,
                }
            )
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "data_source_id": arguments.data_source_id or self.settings.data_source_id,
                "schema_version": self.settings.schema_version,
                "tables": list(tables.values()),
            },
            metadata={"duration_ms": 0, "truncated": outcome.truncated},
        )


class DatabaseQueryTool(RegisteredTool):
    """`database_query`：Tool 输入校验 -> SQL Validator -> read-only 数据库。"""

    metadata = ToolMetadata("database_query", "1", "medium", "structured_query")
    input_model = DatabaseQueryInput

    def __init__(
        self,
        *,
        settings: Settings,
        validator: SQLValidator,
        query_runner: ReadOnlyQueryRunner,
        results: QueryResultRepository,
    ) -> None:
        self.settings = settings
        self.validator = validator
        self.query_runner = query_runner
        self.results = results

    def execute(
        self,
        tool_call_id: str,
        arguments: DatabaseQueryInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        validation = self._validate(arguments.sql)
        if not validation.accepted:
            # 安全与策略拒绝对本调用是终态的，绝不会重试。
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                error=ToolError(
                    code="sql_rejected",
                    message="The query was rejected by the SQL Validator.",
                ),
                metadata={"sql_validation": "rejected", "reasons": list(validation.reasons)},
            )
        max_rows = min(arguments.max_rows, self.settings.sql_max_rows)
        try:
            # 仅多读一行，以便从结果集判断是否发生了截断。
            outcome = self.query_runner.run(
                arguments.sql, arguments.parameters, max_rows=max_rows + 1
            )
        except DatabaseUnavailableError:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code="database_unavailable", message="The database is unavailable."
                ),
                metadata={"sql_validation": "accepted"},
            )
        if outcome.status != "success":
            code = outcome.error_code or "database_error"
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(
                    code=code,
                    message=(
                        "The query exceeded the configured time limit."
                        if code == "query_timeout"
                        else "The query could not be executed safely."
                    ),
                ),
                metadata={"sql_validation": "accepted"},
            )

        # 掩码与截断：敏感列替换为固定掩码，其余值做长度截断，避免泄露原始数据。
        masked_columns = [
            index
            for index, column in enumerate(outcome.columns)
            if column.casefold() in self.settings.masked_column_names
        ]
        rows = [
            [
                _mask(value) if index in masked_columns else _safe_value(value)
                for index, value in enumerate(row)
            ]
            for row in outcome.rows
        ]
        duration_ms = int((time.monotonic() - started) * 1000)
        snapshot = self.results.create(
            user_id=(context.user_id if context and context.user_id else ""),
            conversation_id=context.conversation_id if context else None,
            run_id=context.run_id if context else None,
            data_source_id=arguments.data_source_id or self.settings.data_source_id,
            columns=list(outcome.columns),
            rows=[list(row) for row in rows],
            truncated=outcome.truncated,
            query_fingerprint=_fingerprint(arguments.sql),
            tables=sorted(_tables_of(arguments.sql)),
            duration_ms=duration_ms,
            retention_seconds=self.settings.query_result_retention_seconds,
        )
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "query_result_id": snapshot.query_result_id,
                "columns": list(outcome.columns),
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
                "truncated": outcome.truncated,
                "query_summary": {
                    "tables": sorted(_tables_of(arguments.sql)),
                    "duration_ms": duration_ms,
                },
            },
            metadata={"sql_validation": "accepted", "duration_ms": duration_ms},
        )

    def _validate(self, sql: str):
        validator: Any = self.validator
        return validator.validate(sql)


class DataExportTool(RegisteredTool):
    """高风险 Tool。提案会创建 Approval Request；执行需要已批准的请求。"""

    metadata = ToolMetadata("data_export", "1", "high", "controlled_export")
    input_model = DataExportInput

    def __init__(
        self,
        *,
        settings: Settings,
        approvals: ApprovalRepository,
        results: QueryResultRepository,
        artifacts: ExportArtifactRepository,
    ) -> None:
        self.settings = settings
        self.approvals = approvals
        self.results = results
        self.artifacts = artifacts

    def execute(
        self,
        tool_call_id: str,
        arguments: DataExportInput,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        scope = context or ToolExecutionContext()
        approval_request_id = scope.approval_request_id
        user_id = scope.user_id
        if scope.query_result_id:
            # 已批准的提案绑定了一个不可变快照；恢复执行不能用一个更新的
            # 查询结果将其替换。
            if arguments.query_result_id != scope.query_result_id:
                return self._rejected(
                    tool_call_id,
                    "approval_binding_mismatch",
                    "The approval does not match this export.",
                )
        if arguments.source != "database_query_result":
            return self._rejected(
                tool_call_id, "unsupported_source", "That export source is unsupported."
            )
        if arguments.format != "csv" or arguments.destination != "managed_export_area":
            return self._rejected(
                tool_call_id,
                "unsupported_destination",
                "Only CSV export to the managed export area is supported.",
            )

        query_result = self.results.get(arguments.query_result_id)
        if query_result is None:
            return self._rejected(
                tool_call_id,
                "query_result_not_found",
                "The referenced query result is unavailable.",
            )
        # 快照只能由产生它的作用域使用。未写入所有者（例如直接调用 Tool）
        # 的快照不可转移。
        if query_result.user_id != (user_id or ""):
            return self._rejected(
                tool_call_id,
                "query_result_not_authorized",
                "The referenced query result is unavailable.",
            )
        columns = [
            column
            for column in query_result.columns
            if not arguments.columns or column in arguments.columns
        ]
        if not columns or any(column not in query_result.columns for column in columns):
            return self._rejected(
                tool_call_id, "invalid_columns", "The requested columns are invalid."
            )
        masked = [
            column for column in columns if column.casefold() in self.settings.masked_column_names
        ]
        if masked:
            return self._rejected(
                tool_call_id,
                "sensitive_column_blocked",
                "The requested export includes a restricted column.",
            )
        if query_result.row_count > self.settings.export_max_rows:
            return self._rejected(
                tool_call_id, "export_too_large", "The query result exceeds the export size limit."
            )

        if approval_request_id is None:
            # 审批绑定不可变快照与过期时间；过期后不能用于恢复原始执行。
            approval = self.approvals.create(
                run_id=scope.run_id or "",
                thread_id=scope.thread_id or "",
                conversation_id=scope.conversation_id or "",
                user_id=scope.user_id or "",
                summary={
                    "row_count": query_result.row_count,
                    "columns": columns,
                    "format": arguments.format,
                    "reason": arguments.reason[:200],
                },
                binding={
                    "query_result_id": query_result.query_result_id,
                    "columns": columns,
                    "format": arguments.format,
                    "destination": arguments.destination,
                    "reason": arguments.reason[:200],
                },
                expires_at=_expiry(self.settings.approval_expiry_seconds),
            )
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="rejected",
                data={
                    "approval_lifecycle": "pending",
                    "approval_request_id": approval.approval_request_id,
                    "summary": approval.summary,
                    "expires_at": approval.expires_at.isoformat(),
                },
                error=ToolError(
                    code="approval_required", message="Approval is required before export."
                ),
                metadata={"duration_ms": 0},
            )

        approval = self.approvals.get(approval_request_id)
        if approval is None:
            return self._rejected(
                tool_call_id, "approval_not_found", "The approval request is unavailable."
            )
        if approval.status == "rejected":
            return self._rejected(tool_call_id, "approval_rejected", "The export was not approved.")
        if approval.status == "expired" or approval.is_expired():
            self.approvals.mark_expired(approval.approval_request_id)
            return self._rejected(
                tool_call_id, "approval_expired", "The approval request has expired."
            )
        if approval.status != "approved":
            return self._rejected(
                tool_call_id, "approval_pending", "Approval is still required before export."
            )
        if approval.binding.get("query_result_id") != arguments.query_result_id:
            return self._rejected(
                tool_call_id,
                "approval_binding_mismatch",
                "The approval does not match this export.",
            )

        # 幂等产物：审批请求已有产物时直接返回，重复决策不会生成第二个导出。
        existing = self.artifacts.get_by_approval(approval.approval_request_id)
        if existing is not None:
            return self._completed(tool_call_id, approval, existing.artifact_id, existing.row_count)

        content = _to_csv(columns, query_result.rows, query_result.columns)
        created = self.artifacts.insert_ignore(
            artifact_id=_new_id(),
            approval_request_id=approval.approval_request_id,
            query_result_id=query_result.query_result_id,
            user_id=approval.user_id,
            format="csv",
            row_count=len(query_result.rows),
            content=content,
        )
        artifact = self.artifacts.get_by_approval(approval.approval_request_id)
        if artifact is None:
            return ToolResult(
                tool_id=self.metadata.tool_id,
                tool_call_id=tool_call_id,
                status="failed",
                error=ToolError(code="export_failed", message="The export could not be created."),
            )
        self.approvals.record_artifact(
            approval.approval_request_id, artifact_id=artifact.artifact_id
        )
        return self._completed(
            tool_call_id, approval, artifact.artifact_id, artifact.row_count, created=created
        )

    def _completed(
        self,
        tool_call_id: str,
        approval: Any,
        artifact_id: str,
        row_count: int,
        *,
        created: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="success",
            data={
                "status": "success",
                "approval_lifecycle": "approved",
                "approval_request_id": approval.approval_request_id,
                "artifact_id": artifact_id,
                "row_count": row_count,
                "format": "csv",
            },
            metadata={"duration_ms": 0, "created_now": created},
        )

    def _rejected(self, tool_call_id: str, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_id=self.metadata.tool_id,
            tool_call_id=tool_call_id,
            status="rejected",
            error=ToolError(code=code, message=message),
        )


def _safe_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")[:2000]
    if isinstance(value, str):
        return value[:2000]
    return value


def _mask(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return ""
    return "*" * min(len(text), 8)


def _fingerprint(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _tables_of(sql: str) -> set[str]:
    import sqlglot
    from sqlglot import exp

    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001 - validator already accepted this statement
        return set()
    return {table.name.casefold() for table in statement.find_all(exp.Table) if table.name}


def _to_csv(columns: list[str], rows: list[list[Any]], all_columns: list[str]) -> str:
    indices = [all_columns.index(column) for column in columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row[index] if index < len(row) else None for index in indices])
    return buffer.getvalue()


def _new_id() -> str:
    from backend.domain.identifiers import new_id

    return new_id()


def _expiry(seconds: int):
    from datetime import timedelta

    from backend.domain.identifiers import utc_now

    return utc_now() + timedelta(seconds=seconds)


def build_tool_validator(settings: Settings) -> ProjectSQLValidator:
    from backend.tools.sql_validator import build_validator

    return build_validator(settings)
