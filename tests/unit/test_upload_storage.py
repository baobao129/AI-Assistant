"""受控上传存储（``ManagedUploadStorage``）的有界与原子性测试。

覆盖 CR-RUNTIME-20261009-01 的存储契约：
- 分块写入 → sha256 与字节数可核验，且最终按内容寻址发布；
- 超限/中断不会发布半份文件（只清理暂存件）；
- 同一内容只保留一个对象；
- 受控 key 不接受路径穿越；
- 读取自带二次上限。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.infrastructure.file_storage import ManagedUploadStorage, UploadStorageError


def _storage(tmp_path: Path, *, max_bytes: int = 4096) -> ManagedUploadStorage:
    return ManagedUploadStorage(tmp_path / "uploads", max_bytes=max_bytes)


def _staging_parts(storage: ManagedUploadStorage) -> list[Path]:
    return list((storage.root / ".staging").glob("*.part"))


def _published_files(storage: ManagedUploadStorage) -> list[Path]:
    return [
        path for path in storage.root.rglob("*") if path.is_file() and ".staging" not in path.parts
    ]


def test_streamed_chunks_are_hashed_and_published(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    payload = ("alpha-结构化输出 " * 100).encode("utf-8")

    writer = storage.begin()
    for offset in range(0, len(payload), 64):
        writer.write(payload[offset : offset + 64])
    stored = writer.finish()

    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.byte_size == len(payload)
    assert stored.storage_key == f"{stored.sha256[:2]}/{stored.sha256}"
    assert storage.read_bytes(stored.storage_key, max_bytes=len(payload)) == payload
    assert _staging_parts(storage) == []


def test_over_limit_upload_never_publishes(tmp_path: Path) -> None:
    storage = _storage(tmp_path, max_bytes=10)
    writer = storage.begin()

    with pytest.raises(UploadStorageError) as failure:
        writer.write(b"x" * 11)

    assert str(failure.value) == "document_too_large"
    assert _staging_parts(storage) == []
    assert _published_files(storage) == []


def test_abort_keeps_previously_published_objects(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    kept = storage.store_bytes(b"keep me")

    writer = storage.begin()
    writer.write(b"discard me")
    writer.abort()

    assert storage.exists(kept.storage_key) is True
    assert _staging_parts(storage) == []
    assert len(_published_files(storage)) == 1


def test_same_content_is_stored_once(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    first = storage.store_bytes(b"same content")
    second = storage.store_bytes(b"same content")

    assert first.storage_key == second.storage_key
    assert len(_published_files(storage)) == 1


@pytest.mark.parametrize("key", ["../escape.txt", "/etc/passwd", "a/../../b"])
def test_storage_key_traversal_is_rejected(tmp_path: Path, key: str) -> None:
    storage = _storage(tmp_path)
    with pytest.raises(UploadStorageError):
        storage.resolve(key)


def test_read_bytes_enforces_its_own_bound(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    stored = storage.store_bytes(b"y" * 100)

    with pytest.raises(UploadStorageError):
        storage.read_bytes(stored.storage_key, max_bytes=10)


def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    stored = storage.store_bytes(b"z" * 10)

    assert storage.delete(stored.storage_key) is True
    assert storage.delete(stored.storage_key) is False
    assert storage.exists(stored.storage_key) is False


def test_ensure_capacity_rejects_when_free_space_is_below_the_minimum(tmp_path: Path) -> None:
    # ADR-036 决策 7：余量不足时明确拒绝（409），绝不写到一半磁盘满。
    storage = _storage(tmp_path)
    storage.ensure_capacity(min_free_bytes=0)  # 0 表示不启用门控
    with pytest.raises(UploadStorageError) as failure:
        storage.ensure_capacity(min_free_bytes=10**18)
    assert str(failure.value) == "storage_unavailable"


def test_ensure_capacity_creates_the_storage_root(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "uploads"
    storage = ManagedUploadStorage(root, max_bytes=1024)
    storage.ensure_capacity(min_free_bytes=1)
    assert root.is_dir()
