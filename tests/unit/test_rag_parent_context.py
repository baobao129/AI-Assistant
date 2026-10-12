"""父块展开与 token 预算上下文测试（ADR-038 决策 6/7 / CC-GRAPH-002）。"""

from __future__ import annotations

from backend.application.index_registry import RegistryParentContext
from backend.graph.main import RAGNodes
from backend.infrastructure.config import Settings
from backend.infrastructure.repositories.documents import ChunkRecord
from backend.infrastructure.token_budget import estimate_tokens


def _chunk(
    chunk_id: str,
    *,
    role: str,
    content: str,
    index_version: str = "v1",
    parent_chunk_id: str | None = None,
    section_path: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    block_type: str = "paragraph",
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id="doc-1",
        index_version=index_version,
        parent_chunk_id=parent_chunk_id,
        chunk_role=role,
        ordinal=0,
        content=content,
        location=None,
        label=f"label-{chunk_id}",
        section_path=section_path,
        block_type=block_type,
        page_start=page_start,
        page_end=page_end,
        content_hash=None,
        token_count=estimate_tokens(content),
    )


class _FakeRegistry:
    """只实现 ``chunks_for_context`` 的注册表替身。"""

    def __init__(self, *, children: list[ChunkRecord], parents: list[ChunkRecord]) -> None:
        self._children = children
        self._parents = parents
        self.calls: list[tuple[str, list[str]]] = []

    def chunks_for_context(self, *, index_version: str, child_ids: list[str]):
        self.calls.append((index_version, list(child_ids)))
        return self._children, {parent.chunk_id: parent for parent in self._parents}


class _UnusedTools:
    def invoke(self, *args, **kwargs):  # pragma: no cover - 映射/上下文节点不调用工具
        raise AssertionError("the RAG mapping nodes must not invoke tools")


def _nodes(*, registry=None, **settings_overrides) -> RAGNodes:
    settings = Settings(_env_file=None, **settings_overrides)
    provider = (
        RegistryParentContext(registry=registry, max_parents=settings.max_parent_expansion)
        if registry is not None
        else None
    )
    return RAGNodes(tools=_UnusedTools(), settings=settings, general_llm=None, parents=provider)


def _item(
    chunk_id: str,
    *,
    content: str,
    parent_chunk_id: str | None,
    index_version: str = "v1",
    section_path: str | None = None,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "document_id": "doc-1",
        "parent_chunk_id": parent_chunk_id,
        "index_version": index_version,
        "content": content,
        "score": 0.9,
        "section_path": section_path,
        "page_start": 12,
        "page_end": 13,
        "metadata": {"label": f"label-{chunk_id}", "location": "p12-13#c0"},
    }


# --- RegistryParentContext ------------------------------------------------


def test_parent_context_deduplicates_by_parent_chunk_id():
    parent = _chunk("v1-p0", role="parent", content="父块正文")
    registry = _FakeRegistry(children=[], parents=[parent, parent])
    provider = RegistryParentContext(registry=registry, max_parents=8)
    context = provider.parents_for(index_version="v1", child_ids=["v1-c0", "v1-c1"])
    assert list(context) == ["v1-p0"]


def test_parent_context_drops_records_that_are_not_parents_of_this_version():
    good = _chunk("v1-p0", role="parent", content="真父块")
    wrong_role = _chunk("v1-c9", role="child", content="子块冒充父块")
    wrong_version = _chunk("v2-p0", role="parent", content="别的版本", index_version="v2")
    registry = _FakeRegistry(children=[], parents=[good, wrong_role, wrong_version])
    provider = RegistryParentContext(registry=registry, max_parents=8)
    context = provider.parents_for(index_version="v1", child_ids=["v1-c0"])
    assert list(context) == ["v1-p0"]


def test_parent_context_is_bounded_by_the_configured_expansion_limit():
    parents = [_chunk(f"v1-p{index}", role="parent", content="x") for index in range(10)]
    registry = _FakeRegistry(children=[], parents=parents)
    provider = RegistryParentContext(registry=registry, max_parents=3)
    context = provider.parents_for(index_version="v1", child_ids=["v1-c0"])
    assert len(context) == 3


def test_parent_context_short_circuits_without_ids_or_version():
    registry = _FakeRegistry(children=[], parents=[_chunk("v1-p0", role="parent", content="x")])
    provider = RegistryParentContext(registry=registry, max_parents=8)
    assert provider.parents_for(index_version="v1", child_ids=[]) == {}
    assert provider.parents_for(index_version="", child_ids=["v1-c0"]) == {}
    assert registry.calls == []


# --- parent_child_mapping --------------------------------------------------


def test_parent_child_mapping_attaches_the_real_parent():
    registry = _FakeRegistry(
        children=[],
        parents=[_chunk("v1-p0", role="parent", content="父块正文", page_start=10, page_end=20)],
    )
    nodes = _nodes(registry=registry)
    state = {"retrieval_results": [_item("v1-c0", content="命中内容", parent_chunk_id="v1-p0")]}
    update = nodes.parent_child_mapping(state)
    item = update["retrieval_results"][0]
    assert item["parent"]["chunk_id"] == "v1-p0"
    assert item["parent"]["page_start"] == 10


def test_parent_child_mapping_without_a_provider_keeps_the_hits():
    nodes = _nodes()
    state = {"retrieval_results": [_item("v1-c0", content="命中内容", parent_chunk_id="v1-p0")]}
    update = nodes.parent_child_mapping(state)
    assert update["retrieval_results"][0]["chunk_id"] == "v1-c0"
    assert "parent" not in update["retrieval_results"][0]


def test_parent_child_mapping_survives_a_failing_provider():
    class _BrokenRegistry:
        def chunks_for_context(self, **_kwargs):
            raise RuntimeError("metadata store unavailable")

    nodes = _nodes(registry=_BrokenRegistry())
    state = {"retrieval_results": [_item("v1-c0", content="命中内容", parent_chunk_id="v1-p0")]}
    update = nodes.parent_child_mapping(state)
    # 父块元数据不可用时退化为直通：绝不因为缺结构而丢掉命中的证据。
    assert update["retrieval_results"][0]["chunk_id"] == "v1-c0"


# --- context_build ---------------------------------------------------------


def _mapped(items, parent_content: str, *, parents: dict[str, dict] | None = None):
    parent = {
        "chunk_id": "v1-p0",
        "content": parent_content,
        "label": "父块标签",
        "location": "p10-20#c0",
        "section_path": "第一章",
        "block_type": "paragraph",
        "page_start": 10,
        "page_end": 20,
        "token_count": estimate_tokens(parent_content),
    }
    if parents is not None:
        parent = parents
    return [{**item, "parent": parent} for item in items]


def test_context_keeps_the_hit_evidence_and_bounds_the_parent_background():
    nodes = _nodes(max_context_tokens=200, max_context_chars=2000)
    huge_parent = "父块背景内容" * 500
    items = _mapped(
        [_item("v1-c0", content="真正命中的证据内容", parent_chunk_id="v1-p0")], huge_parent
    )
    context = nodes.context_build({"retrieval_results": items})["context"]
    assert "真正命中的证据内容" in context
    # 大父块只贡献有界背景（预算的 1/4），不会挤掉命中证据。
    assert estimate_tokens(context) <= 200
    assert context.count("父块背景内容") < huge_parent.count("父块背景内容")


def test_context_emits_each_parent_at_most_once():
    nodes = _nodes(max_context_tokens=5000, max_context_chars=20000)
    items = _mapped(
        [
            _item("v1-c0", content="第一处命中", parent_chunk_id="v1-p0"),
            _item("v1-c1", content="第二处命中", parent_chunk_id="v1-p0"),
        ],
        "共享的父块背景",
    )
    context = nodes.context_build({"retrieval_results": items})["context"]
    assert "第一处命中" in context
    assert "第二处命中" in context
    # 同一个父块只出现一次（同父多个命中不重复填上下文）。
    assert context.count("共享的父块背景") == 1


def test_a_huge_parent_cannot_displace_other_evidence():
    nodes = _nodes(max_context_tokens=300, max_context_chars=4000)
    items = _mapped(
        [
            _item("v1-c0", content="甲证据", parent_chunk_id="v1-p0"),
            _item("v1-c1", content="乙证据", parent_chunk_id="v1-p0"),
        ],
        "很长的父块背景" * 800,
    )
    context = nodes.context_build({"retrieval_results": items})["context"]
    assert "甲证据" in context
    assert "乙证据" in context
    assert estimate_tokens(context) <= 300


def test_context_is_bounded_by_both_tokens_and_characters():
    # token 预算很宽，但字符上限很紧：字符上限必须独立生效（两个预算取先到者）。
    nodes = _nodes(max_context_tokens=10000, max_context_chars=500)
    items = [_item("v1-c0", content="内容" * 500, parent_chunk_id=None)]
    context = nodes.context_build({"retrieval_results": items})["context"]
    assert context is not None
    assert len(context) <= 500


def test_context_is_none_without_any_evidence():
    nodes = _nodes()
    assert nodes.context_build({"retrieval_results": []})["context"] is None


def test_context_label_carries_the_real_page_span():
    nodes = _nodes(max_context_tokens=1000, max_context_chars=4000)
    items = _mapped([_item("v1-c0", content="证据内容", parent_chunk_id="v1-p0")], "父块")
    context = nodes.context_build({"retrieval_results": items})["context"]
    assert "p12-13" in context
