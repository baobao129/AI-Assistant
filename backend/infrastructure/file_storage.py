"""上传原文件的受控存储适配器（Infrastructure 边界）。

背景（CR-RUNTIME-20261009-01）：上传此前在 API 层 ``await file.read()`` 把整份文件读入
内存，再以 ``bytes`` 传给 worker；上调上限会同时放大内存、并发与重启丢失风险，且解析失败
的 Job 重试没有任何原始字节可用。本适配器提供：

- **流式写入**：调用方按块写入，内存占用与文件大小无关；
- **内容寻址**：最终 key 为 ``<sha256[:2]>/<sha256>``，同名内容天然复用，不接受客户端路径；
- **原子可见**：先写 ``.staging/<uuid>.part``，``finish()`` 时才 ``os.replace`` 到最终路径，
  因此中断/失败的上传绝不会留下半份文件（``abort()`` 只删暂存件）；
- **有界**：任何时刻超过 ``max_bytes`` 立即失败并清理暂存件。

API 层只持有 ``StoredUpload``（key/hash/size），绝不把文件系统路径暴露给客户端。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

STAGING_DIRNAME = ".staging"


class UploadStorageError(RuntimeError):
    """原文件无法被受控存储接受。"""


@dataclass(frozen=True)
class StoredUpload:
    """受控存储中的一份原文件引用（不含任何用户可控路径）。"""

    storage_key: str
    sha256: str
    byte_size: int


class ManagedUploadStorage:
    """内容寻址的有界原文件存储。"""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    # 写入 ------------------------------------------------------------
    def begin(self) -> _UploadWriter:
        return _UploadWriter(storage=self)

    def store_bytes(self, data: bytes) -> StoredUpload:
        """便捷入口：本地脚本/测试把已知字节写入受控存储（仍受有界限制）。"""

        writer = self.begin()
        writer.write(data)
        return writer.finish()

    def ensure_capacity(self, *, min_free_bytes: int) -> None:
        """上传前的余量门控（ADR-036 决策 7）：余量不足时明确拒绝，而不是写到一半磁盘满。"""

        if min_free_bytes <= 0:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.root).free
        except OSError as error:
            raise UploadStorageError("storage_unavailable") from error
        if free < min_free_bytes:
            raise UploadStorageError("storage_unavailable")

    # 读取 ------------------------------------------------------------
    def resolve(self, storage_key: str) -> Path:
        """把受控 key 解析为绝对路径；拒绝任何越界或非法 key。"""

        candidate = (self.root / storage_key).resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            raise UploadStorageError("invalid_storage_key")
        return candidate

    def exists(self, storage_key: str) -> bool:
        try:
            return self.resolve(storage_key).is_file()
        except UploadStorageError:
            return False

    def read_bytes(self, storage_key: str, *, max_bytes: int) -> bytes:
        """读取原文件字节；超过给定上限时安全失败（调用方已有自己的上限）。"""

        path = self.resolve(storage_key)
        try:
            size = path.stat().st_size
        except OSError as error:
            raise UploadStorageError("stored_file_missing") from error
        if size > max_bytes:
            raise UploadStorageError("stored_file_too_large")
        try:
            return path.read_bytes()
        except OSError as error:
            raise UploadStorageError("stored_file_unreadable") from error

    # 删除 ------------------------------------------------------------
    def delete(self, storage_key: str) -> bool:
        try:
            path = self.resolve(storage_key)
        except UploadStorageError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as error:  # pragma: no cover - 权限/句柄异常
            logger.warning("upload_delete_failed: %s", type(error).__name__)
            return False


class _UploadWriter:
    """单次上传的写入器：流式累计 hash/size，``finish()`` 原子发布。"""

    def __init__(self, *, storage: ManagedUploadStorage) -> None:
        self._storage = storage
        staging_dir = storage.root / STAGING_DIRNAME
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._path = staging_dir / f"{uuid.uuid4().hex}.part"
        self._handle = self._path.open("wb")
        self._digest = hashlib.sha256()
        self._size = 0
        self._finished = False

    def write(self, chunk: bytes) -> None:
        if self._finished:
            raise UploadStorageError("upload_already_finished")
        self._size += len(chunk)
        if self._size > self._storage.max_bytes:
            # 超限立即失败并清理，绝不把超限文件留在存储中。
            self.abort()
            raise UploadStorageError("document_too_large")
        self._digest.update(chunk)
        try:
            self._handle.write(chunk)
        except OSError as error:
            self.abort()
            raise UploadStorageError("storage_write_failed") from error

    def finish(self) -> StoredUpload:
        if self._finished:
            raise UploadStorageError("upload_already_finished")
        self._finished = True
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as error:
            self.abort()
            raise UploadStorageError("storage_write_failed") from error
        finally:
            self._handle.close()
        sha256 = self._digest.hexdigest()
        storage_key = f"{sha256[:2]}/{sha256}"
        target = self._storage.resolve(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 原子发布：同名内容互相覆盖是幂等的（内容寻址）。
            os.replace(self._path, target)
        except OSError as error:
            self.abort()
            raise UploadStorageError("storage_publish_failed") from error
        return StoredUpload(storage_key=storage_key, sha256=sha256, byte_size=self._size)

    def abort(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._handle.close()
        except OSError:  # pragma: no cover - 关闭失败不影响清理
            pass
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:  # pragma: no cover
            logger.warning("upload_staging_cleanup_failed: %s", type(error).__name__)


__all__ = ["ManagedUploadStorage", "StoredUpload", "UploadStorageError"]
