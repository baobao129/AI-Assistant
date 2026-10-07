"""Checkpoint 上下文管理器在 memory / sqlite 两种模式下的初始化验证。"""

from pathlib import Path

from backend.domain.checkpoint import checkpoint_context


def test_memory_checkpoint_initializes():
    with checkpoint_context("memory") as saver:
        assert saver is not None


def test_sqlite_checkpoint_initializes(tmp_path: Path):
    # sqlite 模式会在磁盘落盘：退出上下文后文件应真实存在。
    path = tmp_path / "checkpoints.sqlite"
    with checkpoint_context("sqlite", path) as saver:
        assert saver is not None
    assert path.exists()
