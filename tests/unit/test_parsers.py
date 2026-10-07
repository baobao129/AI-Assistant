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
    ) -> None:
        self.HTTPError = real_httpx.HTTPError
        self.calls: list[tuple] = []
        self._post = post_response
        self._put = put_response or _FakeResponse()
        self._poll = list(poll_responses or [])
        self._default_poll = default_poll
        self._zip = zip_response or _FakeResponse()

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
    assert kinds == ["post", "put", "get", "get", "get"]
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
    )
    parser = create_parser(settings)
    assert isinstance(parser, MinerUParser)
    assert parser.model_version == "vlm"
    assert parser.api_key == "token-1"
