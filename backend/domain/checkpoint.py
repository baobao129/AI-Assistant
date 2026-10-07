from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

CheckpointMode = Literal["memory", "sqlite"]


@contextmanager
def checkpoint_context(
    mode: CheckpointMode, sqlite_path: Path | str | None = None
) -> Iterator[object]:
    """Expose approved Memory and SQLite modes without making Checkpoint a Graph Node."""
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
) -> Iterator[object]:
    """Long-lived Checkpoint handle for a process; closed when the runtime stops."""

    with checkpoint_context(mode, sqlite_path) as saver:
        yield saver
