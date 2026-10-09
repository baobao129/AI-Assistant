from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

CheckpointMode = Literal["memory", "sqlite"]


@contextmanager
def checkpoint_context(
    mode: CheckpointMode, sqlite_path: Path | str | None = None
) -> Generator[object, None, None]:
    """提供已批准的 Memory 与 SQLite 模式，同时不把 Checkpoint 变成 Graph Node。"""
    if mode == "memory":
        yield InMemorySaver()
        return
    if sqlite_path is None:
        raise ValueError("sqlite_path is required for SQLite checkpoints")
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        saver.setup()
        yield saver


@contextmanager
def open_checkpointer(
    mode: CheckpointMode, sqlite_path: Path | str | None = None
) -> Generator[object, None, None]:
    """进程级长期存活的 Checkpoint 句柄；运行时停止时关闭。"""

    with checkpoint_context(mode, sqlite_path) as saver:
        yield saver
