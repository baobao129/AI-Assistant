"""Isolated native Chinese BM25 probe; cleanup only the collection created here."""

import os

import pytest
from pymilvus import AnnSearchRequest, RRFRanker

from backend.domain.identifiers import new_id
from backend.infrastructure.vector_store import MilvusVectorStore, VectorRecord


@pytest.mark.live_db
def test_native_bm25_filters_all_authorized_versions_before_global_rank():
    uri = os.environ.get("TEST_MILVUS_URI")
    if not uri:
        pytest.skip("TEST_MILVUS_URI is not configured")
    store = MilvusVectorStore(uri=uri, timeout_seconds=30)
    name = f"acceptance_{new_id()}_hybrid_v1"
    try:
        store.ensure_collection(name=name, dimension=8)
        store.upsert(
            collection=name,
            records=[
                VectorRecord(
                    "a", "allowed", "v1", None, "采购审批上限金额为5000元。", "a", None, [1.0] * 8
                ),
                VectorRecord(
                    "b", "allowed2", "v2", None, "销售收入为9000元。", "b", None, [1.0] * 8
                ),
                VectorRecord(
                    "c", "foreign", "v3", None, "采购审批上限金额为5000元。", "c", None, [1.0] * 8
                ),
            ],
        )
        store.client.flush(name)
        hits = store.search_scope(
            collection=name,
            embedding=[1.0] * 8,
            query="采购审批上限",
            versions=("v1", "v2"),
            top_k=10,
            sparse=True,
        )
        assert hits and hits[0].chunk_id == "a"
        assert all(hit.index_version in {"v1", "v2"} for hit in hits)
        # Capability probe of the locked native fusion API; production uses explicit global ranks.
        fused = store.client.hybrid_search(
            collection_name=name,
            reqs=[
                AnnSearchRequest(
                    [[1.0] * 8],
                    "embedding",
                    {"metric_type": "COSINE"},
                    10,
                    expr='index_version in ["v1","v2"]',
                ),
                AnnSearchRequest(
                    ["采购审批上限"],
                    "sparse_embedding",
                    {"metric_type": "BM25"},
                    10,
                    expr='index_version in ["v1","v2"]',
                ),
            ],
            ranker=RRFRanker(k=60),
            limit=10,
            output_fields=["index_version"],
        )
        assert fused[0] and all(hit["entity"]["index_version"] in {"v1", "v2"} for hit in fused[0])
        store.ensure_collection(name=name, dimension=8)
    finally:
        store.client.drop_collection(name)
        store.close()
