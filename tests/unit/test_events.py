"""Run Event sequencing, replay, and SSE wire-format tests."""

from __future__ import annotations

import json
import queue
import threading

import pytest

from backend.application.events import (
    SSE_EVENT_TYPES,
    RunEventPublisher,
    RunStreamNotFound,
)


class FakeRunEventRepository:
    """In-memory stand-in for the Run Event replay store."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[str, int, str, str, dict, object]]] = {}
        self.trimmed: list[tuple[str, int]] = []

    def append(self, *, run_id, event_id, event_type, thread_id, payload, created_at=None) -> int:
        """Reserve the next per-Run sequence, mirroring the SQL FOR UPDATE contract."""

        rows = self.rows.setdefault(run_id, [])
        sequence = max((int(row[1]) for row in rows), default=0) + 1
        rows.append((event_id, sequence, event_type, thread_id, payload, created_at))
        return sequence

    def list_after(self, *, run_id, after_event_id):
        rows = self.rows.get(run_id, [])
        start = 0
        if after_event_id:
            match = next((row for row in rows if row[0] == after_event_id), None)
            if match is None:
                raise KeyError(after_event_id)
            start = int(match[1])
        return [row for row in rows if int(row[1]) > start]

    def max_sequence(self, run_id):
        rows = self.rows.get(run_id, [])
        return max((int(row[1]) for row in rows), default=0)

    def trim(self, *, run_id, keep):
        self.trimmed.append((run_id, keep))
        return 0


def _publisher() -> tuple[RunEventPublisher, FakeRunEventRepository]:
    repository = FakeRunEventRepository()
    return RunEventPublisher(repository=repository, retention_events=100), repository


def test_sequence_starts_at_one_and_increases_per_run():
    publisher, _ = _publisher()
    first = publisher.publish(
        run_id="r1", thread_id="t1", event_type="agent_start", payload={"status": "running"}
    )
    second = publisher.publish(
        run_id="r1", thread_id="t1", event_type="intent", payload={"intent": "general"}
    )
    other_run = publisher.publish(
        run_id="r2", thread_id="t1", event_type="agent_start", payload={"status": "running"}
    )
    assert (first.sequence, second.sequence, other_run.sequence) == (1, 2, 1)
    assert len({first.event_id, second.event_id, other_run.event_id}) == 3


def test_rejects_events_outside_the_contract_type_list():
    publisher, _ = _publisher()
    with pytest.raises(ValueError):
        publisher.publish(run_id="r1", thread_id="t1", event_type="hidden_reasoning", payload={})
    assert "final" in SSE_EVENT_TYPES


def test_replay_returns_events_after_the_requested_event():
    publisher, _ = _publisher()
    first = publisher.publish(run_id="r1", thread_id="t1", event_type="agent_start", payload={})
    publisher.publish(run_id="r1", thread_id="t1", event_type="intent", payload={})
    publisher.publish(run_id="r1", thread_id="t1", event_type="final", payload={"content": "done"})
    replayed = publisher.history(run_id="r1", after_event_id=first.event_id)
    assert [event.event_type for event in replayed] == ["intent", "final"]
    with pytest.raises(RunStreamNotFound):
        publisher.history(run_id="r1", after_event_id="unknown-event")


def test_sse_wire_format_carries_the_complete_envelope():
    publisher, _ = _publisher()
    envelope = publisher.publish(
        run_id="r1", thread_id="t1", event_type="token", payload={"delta": "hi"}
    )
    wire = envelope.to_sse()
    lines = wire.splitlines()
    assert lines[0] == f"id: {envelope.event_id}"
    assert lines[1] == "event: token"
    assert lines[2].startswith("data: ")
    assert wire.endswith("\n\n")
    parsed = json.loads(lines[2][6:])
    assert parsed == envelope.as_dict()
    assert set(parsed) == {
        "event_id",
        "sequence",
        "thread_id",
        "run_id",
        "event_type",
        "timestamp",
        "payload",
    }


def test_subscription_receives_events_and_closes_after_final():
    publisher, _ = _publisher()
    subscription = publisher.subscribe(run_id="r1")
    thread = threading.Thread(
        target=lambda: (
            publisher.publish(run_id="r1", thread_id="t1", event_type="agent_start", payload={}),
            publisher.publish(
                run_id="r1", thread_id="t1", event_type="final", payload={"content": "x"}
            ),
        ),
        daemon=True,
    )
    thread.start()
    first = subscription.next(timeout=5)
    second = subscription.next(timeout=5)
    assert first is not None and first.event_type == "agent_start"
    assert second is not None and second.event_type == "final"
    assert subscription.next(timeout=1) is None
    assert subscription.closed is True
    thread.join(timeout=5)


def test_subscription_timeout_returns_none_for_heartbeats():
    publisher, _ = _publisher()
    subscription = publisher.subscribe(run_id="empty")
    assert subscription.next(timeout=0.05) is None
    assert subscription.closed is False
    subscription.close()
    assert subscription.closed is True


def test_trim_is_requested_at_the_retention_boundary():
    """Retention is maintained periodically (every 50 published events within a Run)."""

    repository = FakeRunEventRepository()
    publisher = RunEventPublisher(repository=repository, retention_events=50)
    for _ in range(49):
        publisher.publish(run_id="r1", thread_id="t1", event_type="token", payload={})
    assert repository.trimmed == []
    publisher.publish(run_id="r1", thread_id="t1", event_type="token", payload={})
    assert repository.trimmed == [("r1", 50)]


def test_publish_never_blocks_when_no_subscriber_exists():
    publisher, _ = _publisher()
    envelope = publisher.publish(run_id="r1", thread_id="t1", event_type="final", payload={})
    assert envelope.sequence == 1
    assert publisher.is_stream_closed("r1") is True


_ = queue
