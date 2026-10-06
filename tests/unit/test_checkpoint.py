from pathlib import Path

from backend.domain.checkpoint import checkpoint_context


def test_memory_checkpoint_initializes():
    with checkpoint_context("memory") as saver:
        assert saver is not None


def test_sqlite_checkpoint_initializes(tmp_path: Path):
    path = tmp_path / "checkpoints.sqlite"
    with checkpoint_context("sqlite", path) as saver:
        assert saver is not None
    assert path.exists()
