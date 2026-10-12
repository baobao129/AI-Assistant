"""Parser 适配器与 Parent/Child 分块测试。"""

import io
import zipfile

import httpx as real_httpx
import pytest

from backend.infrastructure.config import Settings
from backend.infrastructure.parsers import (
    LightweightParser,
    MinerUParser,
    ParserError,
    RoutedParser,
    chunk_document,
    create_parser,
    supported_content_type,
)


def parse_markdown(text: str):
    return LightweightParser(max_bytes=1024 * 1024).parse(
        filename="policy.md", content_type="text/markdown", data=text.encode("utf-8")
    )


def test_lightweight_parser_normalizes_text():
    parsed = parse_markdown("Title\n\n\n\nBody   with   spaces\n")
    assert parsed.parser == "lightweight"
    assert "Body with spaces" in parsed.text
    assert "\n\n\n" not in parsed.text
    assert parsed.truncated is False


def test_lightweight_parser_rejects_empty_and_oversized_documents():
    parser = LightweightParser(max_bytes=16)
    with pytest.raises(ParserError):
        parser.parse(filename="a.txt", content_type="text/plain", data=b"")
    with pytest.raises(ParserError):
        parser.parse(filename="a.txt", content_type="text/plain", data=b"x" * 64)


def test_lightweight_parser_rejects_binary_content():
    with pytest.raises(ParserError):
        LightweightParser(max_bytes=1024).parse(
            filename="a.txt", content_type="text/plain", data=b"\xff\xfe\x00\x01"
        )


def test_csv_and_json_are_converted_to_text():
    parser = LightweightParser(max_bytes=4096)
    csv_document = parser.parse(filename="rows.csv", content_type="text/csv", data=b"a,b\n1,2\n")
    assert "a | b" in csv_document.text
    json_document = parser.parse(
        filename="data.json", content_type="application/json", data=b'{"k": "v"}'
    )
    assert '"k": "v"' in json_document.text


def test_supported_content_types():
    assert supported_content_type("text/markdown", "a.md")
    assert supported_content_type("application/octet-stream", "a.txt")
    assert not supported_content_type("application/pdf", "a.pdf")


def test_chunking_creates_bounded_parents_and_overlapping_children():
    # Parent 上限约为 parent_chars 的两倍，Child 需带上重叠以满足上下文连续性。
    text = "\n\n".join(f"Paragraph {index} " + "word " * 60 for index in range(6))
    chunked = chunk_document(text, parent_chars=900, child_chars=300, overlap_chars=60)
    assert chunked.parents, "parents must be produced"
    assert chunked.children, "children must be produced"
    for parent in chunked.parents:
        assert len(parent.content) <= 1800
        assert parent.child_ordinals, "every parent maps to at least one child"
    for child in chunked.children:
        assert len(child.content) <= 300
    assert {child.parent_ordinal for child in chunked.children} == {
        parent.ordinal for parent in chunked.parents
    }


def test_chunking_short_document_produces_one_parent():
    chunked = chunk_document(
        "Only one short section.", parent_chars=800, child_chars=200, overlap_chars=40
    )
    assert len(chunked.parents) == 1
    assert len(chunked.children) == 1
    assert chunked.children[0].parent_ordinal == 0


def test_chunking_hard_splits_an_oversized_paragraph():
    chunked = chunk_document("x" * 5000, parent_chars=1000, child_chars=400, overlap_chars=50)
    assert all(len(parent.content) <= 2000 for parent in chunked.parents)
    assert len(chunked.parents) >= 3


# ---- MinerU v4 云适配器（离线替身，绝不发真实请求）--------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON payload")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise real_httpx.HTTPError(f"fake status {self.status_code}")


class _FakeStreamResponse:
    """``httpx.stream`` 的响应替身：支持有界迭代读取与 headers。"""

    def __init__(self, *, content: bytes, status_code: int = 200, headers=None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise real_httpx.HTTPError(f"fake status {self.status_code}")

    def iter_bytes(self, chunk_size: int | None = None):
        size = chunk_size or 65536
        for start in range(0, len(self.content), size):
            yield self.content[start : start + size]


class _FakeHTTPX:
    """记录调用并回放预置响应的 httpx 替身。"""

    def __init__(
        self,
        *,
        post_response: _FakeResponse,
        put_response: _FakeResponse | None = None,
        poll_responses=None,
        default_poll: _FakeResponse | None = None,
        zip_response: _FakeResponse | None = None,
        zip_headers: dict | None = None,
    ) -> None:
        self.HTTPError = real_httpx.HTTPError
        self.calls: list[tuple] = []
        self._post = post_response
        self._put = put_response or _FakeResponse()
        self._poll = list(poll_responses or [])
        self._default_poll = default_poll
        self._zip = zip_response or _FakeResponse()
        self._zip_headers = zip_headers or {}

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("post", url, dict(headers or {}), json))
        return self._post

    def put(self, url, content=None, timeout=None):
        self.calls.append(("put", url, {}, content))
        return self._put

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", url, dict(headers or {}), None))
        if self._poll:
            return self._poll.pop(0)
        if self._default_poll is not None:
            return self._default_poll
        return self._zip

    def stream(self, method, url, timeout=None, follow_redirects=None):
        self.calls.append(("stream", url, {}, None))
        content = getattr(self._zip, "content", b"")
        response = _FakeStreamResponse(content=content, headers=dict(self._zip_headers))
        return _FakeStream(response)


class _FakeStream:
    """``httpx.stream`` 的上下文管理器替身。"""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, *exc_info) -> bool:
        return False


def _zip_with_markdown(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", text)
    return buffer.getvalue()


def _mineru_parser(*, api_key: str = "test-token", timeout: float = 5.0) -> MinerUParser:
    return MinerUParser(
        base_url="https://mineru.net/api/v4",
        api_key=api_key,
        model_version="pipeline",
        timeout_seconds=timeout,
        max_bytes=1024 * 1024,
        poll_interval_seconds=0.01,
    )


def test_mineru_parser_uploads_polls_and_reads_markdown(monkeypatch):
    fake = _FakeHTTPX(
        post_response=_FakeResponse(
            payload={"code": 0, "data": {"batch_id": "b-1", "file_urls": ["https://up.example/p"]}}
        ),
        poll_responses=[
            _FakeResponse(payload={"code": 0, "data": {"extract_result": [{"state": "running"}]}}),
            _FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://cdn.example/full.zip"}
                        ]
                    },
                }
            ),
        ],
        zip_response=_FakeResponse(content=_zip_with_markdown("# MinerU Result\n\nBody   text\n")),
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)

    parsed = _mineru_parser().parse(
        filename="paper.pdf", content_type="application/pdf", data=b"%PDF-fake"
    )

    assert parsed.parser == "mineru"
    assert "MinerU Result" in parsed.text
    assert "Body text" in parsed.text  # 文本经过规范化
    kinds = [call[0] for call in fake.calls]
    assert kinds == ["post", "put", "get", "get", "stream"]
    # 预签名上传绝不能携带 Authorization（官方文档：双重鉴权会被 403 拒绝）。
    assert "Authorization" not in fake.calls[1][2]
    # 申请上传与轮询必须携带 Bearer Token。
    assert fake.calls[0][2].get("Authorization") == "Bearer test-token"
    assert fake.calls[2][2].get("Authorization") == "Bearer test-token"


def test_mineru_parser_maps_failed_state_to_parse_error(monkeypatch):
    fake = _FakeHTTPX(
        post_response=_FakeResponse(
            payload={"code": 0, "data": {"batch_id": "b-2", "file_urls": ["https://up.example/p"]}}
        ),
        poll_responses=[
            _FakeResponse(
                payload={
                    "code": 0,
                    "data": {"extract_result": [{"state": "failed", "err_msg": "bad file"}]},
                }
            )
        ],
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    with pytest.raises(ParserError):
        _mineru_parser().parse(filename="x.pdf", content_type="application/pdf", data=b"%PDF")


def test_mineru_parser_requires_an_api_key_and_never_calls_out(monkeypatch):
    fake = _FakeHTTPX(post_response=_FakeResponse(payload={"code": 0, "data": {}}))
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    with pytest.raises(ParserError):
        _mineru_parser(api_key="").parse(
            filename="x.pdf", content_type="application/pdf", data=b"%PDF"
        )
    assert fake.calls == []


def test_mineru_parser_polling_is_bounded_by_timeout(monkeypatch):
    running = _FakeResponse(payload={"code": 0, "data": {"extract_result": [{"state": "running"}]}})
    fake = _FakeHTTPX(
        post_response=_FakeResponse(
            payload={"code": 0, "data": {"batch_id": "b-3", "file_urls": ["https://up.example/p"]}}
        ),
        default_poll=running,
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    with pytest.raises(ParserError):
        _mineru_parser(timeout=0.05).parse(
            filename="x.pdf", content_type="application/pdf", data=b"%PDF"
        )
    # 轮询有界：不会无限查询（每个间隔 0.01s、总时限 0.05s）。
    assert len([call for call in fake.calls if call[0] == "get"]) < 20


def test_mineru_parser_keeps_the_batch_id_and_resumes_after_a_timeout(monkeypatch):
    # 轮询超时 ≠ 任务未创建：batch id 必须保留，续轮询同一任务即可拿到结果（ADR-037）。
    post = _FakeResponse(
        payload={"code": 0, "data": {"batch_id": "b-resume", "file_urls": ["https://up.example/p"]}}
    )
    running = _FakeResponse(payload={"code": 0, "data": {"extract_result": [{"state": "running"}]}})
    monkeypatch.setattr(
        "backend.infrastructure.parsers.httpx", _FakeHTTPX(post_response=post, default_poll=running)
    )
    parser = _mineru_parser(timeout=0.05)
    task = parser.submit(filename="a.pdf", content_type="application/pdf", data=b"%PDF-fake")
    assert task.batch_id == "b-resume"
    with pytest.raises(ParserError) as failure:
        parser.fetch(task.batch_id)
    # 超时有独立的安全码，与「提交失败」区分开。
    assert str(failure.value) == "parse_timeout"

    done = _FakeResponse(
        payload={
            "code": 0,
            "data": {
                "extract_result": [{"state": "done", "full_zip_url": "https://cdn.example/z.zip"}]
            },
        }
    )
    resume_fake = _FakeHTTPX(
        post_response=post,
        poll_responses=[done],
        zip_response=_FakeResponse(content=_zip_with_markdown("# Resumed\n")),
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", resume_fake)
    parsed = parser.fetch("b-resume", title="a.pdf")

    assert "Resumed" in parsed.text
    # 续轮询只有查询与下载：绝不重复提交（否则会重复消耗额度）。
    assert [call[0] for call in resume_fake.calls] == ["get", "stream"]


def test_mineru_suffixes_match_the_measured_provider_capability_matrix():
    # ADR-037 实测结论：xlsx/docx/pptx 支持；html 被 pipeline 模型版本在提交阶段拒绝；
    # xls（BIFF8）未获证明，保持拒绝。
    for suffix in (".pdf", ".docx", ".pptx", ".xlsx", ".png"):
        assert supported_content_type(
            "application/octet-stream", f"a{suffix}", allow_remote_parser=True
        ), suffix
    for suffix in (".html", ".htm", ".xls"):
        assert not supported_content_type(
            "application/octet-stream", f"a{suffix}", allow_remote_parser=True
        ), suffix
    # HTML 即使带着 text/* 的 content type 也要在提交前明确拒绝。
    assert not supported_content_type("text/html", "page.html", allow_remote_parser=True)
    assert not supported_content_type("text/html", "page.html")


def test_mineru_suffixes_are_gated_by_parser_provider():
    # 轻量解析器不接受 PDF/图片；仅当 parser_provider=mineru 时放行其官方支持类型。
    assert not supported_content_type("application/pdf", "a.pdf")
    assert supported_content_type("application/pdf", "a.pdf", allow_remote_parser=True)
    assert not supported_content_type("image/png", "a.png")
    assert supported_content_type("image/png", "a.png", allow_remote_parser=True)
    assert supported_content_type("text/markdown", "a.md", allow_remote_parser=True)


def test_create_parser_wires_mineru_settings():
    settings = Settings(
        _env_file=None,
        parser_provider="mineru",
        mineru_base_url="https://mineru.net/api/v4",
        mineru_api_key="token-1",
        mineru_model_version="vlm",
        mineru_max_bytes=2048,
    )
    parser = create_parser(settings)
    # MinerU 档位返回路由 parser：文本类走本地，其余走远端（ADR-036 决策 5）。
    assert isinstance(parser, RoutedParser)
    assert isinstance(parser.remote, MinerUParser)
    assert parser.remote.model_version == "vlm"
    assert parser.remote.api_key == "token-1"
    # 远端 adapter 收到的是**远端单份硬限**，不是本地接受上限。
    assert parser.remote.max_bytes == 2048
    assert parser.provider_for(filename="a.md", content_type="text/markdown") == "lightweight"
    assert parser.provider_for(filename="a.pdf", content_type="application/pdf") == "mineru"


def test_create_parser_without_mineru_returns_local_parser():
    settings = Settings(_env_file=None, parser_provider="lightweight")
    parser = create_parser(settings)
    assert isinstance(parser, LightweightParser)
    assert parser.provider_for(filename="a.pdf", content_type="application/pdf") == "lightweight"


# ---- ADR-036：显式预算、远端硬限与结果归档边界 ---------------------------------


def test_lightweight_parser_fails_explicitly_instead_of_truncating():
    parser = LightweightParser(max_bytes=1024 * 1024, max_chars=50)
    with pytest.raises(ParserError) as failure:
        parser.parse(filename="a.md", content_type="text/markdown", data=("x" * 200).encode())
    assert str(failure.value) == "extracted_text_too_large"


def test_routed_parser_sends_text_locally_and_documents_remotely(monkeypatch):
    fake = _FakeHTTPX(
        post_response=_FakeResponse(
            payload={"code": 0, "data": {"batch_id": "b-r", "file_urls": ["https://up.example/p"]}}
        ),
        poll_responses=[
            _FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://cdn.example/full.zip"}
                        ]
                    },
                }
            )
        ],
        zip_response=_FakeResponse(content=_zip_with_markdown("# Remote\n")),
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    parser = RoutedParser(
        local=LightweightParser(max_bytes=1024 * 1024, max_chars=1000),
        remote=_mineru_parser(),
    )
    # 文本类：本地解析，绝不触网（MinerU v4 不支持文本类，此前会必然失败）。
    local_parsed = parser.parse(
        filename="notes.md", content_type="text/markdown", data=b"# Local\n\nBody"
    )
    assert local_parsed.parser == "lightweight"
    assert fake.calls == []
    # 其余类型：走远端 provider。
    remote_parsed = parser.parse(
        filename="paper.pdf", content_type="application/pdf", data=b"%PDF-fake"
    )
    assert remote_parsed.parser == "mineru"
    assert [call[0] for call in fake.calls] == ["post", "put", "get", "stream"]


def test_mineru_parser_rejects_files_above_the_provider_limit_without_calling_out(monkeypatch):
    fake = _FakeHTTPX(post_response=_FakeResponse(payload={"code": 0, "data": {}}))
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    parser = MinerUParser(
        base_url="https://mineru.net/api/v4",
        api_key="token",
        model_version="pipeline",
        timeout_seconds=5.0,
        max_bytes=10,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(ParserError) as failure:
        parser.parse(filename="big.pdf", content_type="application/pdf", data=b"x" * 64)
    assert str(failure.value) == "document_exceeds_parser_limit"
    assert fake.calls == []


def test_mineru_parser_bounds_zip_entries_and_paths(monkeypatch):
    def parser_with(**kwargs) -> MinerUParser:
        return MinerUParser(
            base_url="https://mineru.net/api/v4",
            api_key="token",
            model_version="pipeline",
            timeout_seconds=5.0,
            max_bytes=1024 * 1024,
            poll_interval_seconds=0.01,
            **kwargs,
        )

    poll = _FakeResponse(
        payload={
            "code": 0,
            "data": {
                "extract_result": [
                    {"state": "done", "full_zip_url": "https://cdn.example/full.zip"}
                ]
            },
        }
    )
    post = _FakeResponse(
        payload={"code": 0, "data": {"batch_id": "b-z", "file_urls": ["https://up.example/p"]}}
    )

    # 目录越界：整份结果被拒绝。
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../evil.md", "# Evil")
    monkeypatch.setattr(
        "backend.infrastructure.parsers.httpx",
        _FakeHTTPX(
            post_response=post,
            poll_responses=[poll],
            zip_response=_FakeResponse(content=traversal.getvalue()),
        ),
    )
    with pytest.raises(ParserError) as traversal_failure:
        parser_with().parse(filename="a.pdf", content_type="application/pdf", data=b"%PDF")
    assert str(traversal_failure.value) == "parser_result_unsafe"

    # 条目数超限：由 parser 的上限参数控制。
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as archive:
        for index in range(6):
            archive.writestr(f"part-{index}.md", "# part")
    monkeypatch.setattr(
        "backend.infrastructure.parsers.httpx",
        _FakeHTTPX(
            post_response=post,
            poll_responses=[poll],
            zip_response=_FakeResponse(content=many.getvalue()),
        ),
    )
    with pytest.raises(ParserError) as entries_failure:
        parser_with(max_zip_entries=5).parse(
            filename="a.pdf", content_type="application/pdf", data=b"%PDF"
        )
    assert str(entries_failure.value) == "parser_result_too_large"

    # 声明长度超限：下载前即拒绝。
    oversized = _FakeHTTPX(
        post_response=post,
        poll_responses=[poll],
        zip_response=_FakeResponse(content=_zip_with_markdown("# ok")),
        zip_headers={"content-length": "999999999999"},
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", oversized)
    with pytest.raises(ParserError) as declared_failure:
        parser_with().parse(filename="a.pdf", content_type="application/pdf", data=b"%PDF")
    assert str(declared_failure.value) == "parser_result_too_large"

    # 归档内 Markdown 条目超过上限。
    big_entry = _FakeHTTPX(
        post_response=post,
        poll_responses=[poll],
        zip_response=_FakeResponse(content=_zip_with_markdown("x" * 512)),
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", big_entry)
    with pytest.raises(ParserError) as entry_failure:
        MinerUParser(
            base_url="https://mineru.net/api/v4",
            api_key="token",
            model_version="pipeline",
            timeout_seconds=5.0,
            max_bytes=1024 * 1024,
            poll_interval_seconds=0.01,
            max_markdown_entry_bytes=64,
        ).parse(filename="a.pdf", content_type="application/pdf", data=b"%PDF")
    assert str(entry_failure.value) == "parser_result_too_large"


def test_mineru_parser_rejects_unsupported_result_url_scheme(monkeypatch):
    fake = _FakeHTTPX(
        post_response=_FakeResponse(
            payload={"code": 0, "data": {"batch_id": "b-s", "file_urls": ["https://up.example/p"]}}
        ),
        poll_responses=[
            _FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "extract_result": [{"state": "done", "full_zip_url": "file:///etc/passwd"}]
                    },
                }
            )
        ],
    )
    monkeypatch.setattr("backend.infrastructure.parsers.httpx", fake)
    with pytest.raises(ParserError) as failure:
        _mineru_parser().parse(filename="a.pdf", content_type="application/pdf", data=b"%PDF")
    assert str(failure.value) == "parser_result_unsafe"
