"""SSE Application Event delivery and replay (API-005).

Events are the transport/observability record. They never mutate Graph State, and
the replay store is the Run Event store, not Conversation Persistence.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.domain.identifiers import new_id, to_rfc3339, utc_now
from backend.infrastructure.repositories.conversation import RunEventRepository

SSE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_start",
        "intent",
        "retrieval",
        "tool_call",
        "tool_result",
        "token",
        "citation",
        "approval_required",
        "final",
        "error",
    }
)

TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"final", "error"})

_END = object()


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    sequence: int
    thread_id: str
    run_id: str
    event_type: str
    timestamp: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_sse(self) -> str:
        data = json.dumps(self.as_dict(), ensure_ascii=False)
        return f"id: {self.event_id}\nevent: {self.event_type}\ndata: {data}\n\n"


@dataclass
class _RunChannel:
    queue: queue.Queue[Any] = field(default_factory=queue.Queue)
    closed: bool = False
    subscribers: int = 0


class RunStreamNotFound(Exception):
    """Replay was requested for a Run whose stream is unavailable."""


class RunEventPublisher:
    """Assigns per-Run sequences, persists for replay, and fans out to live listeners."""

    def __init__(self, *, repository: RunEventRepository, retention_events: int = 1000) -> None:
        self.repository = repository
        self.retention_events = retention_events
        self._channels: dict[str, _RunChannel] = {}
        self._lock = threading.Lock()

    def _channel(self, run_id: str) -> _RunChannel:
        with self._lock:
            channel = self._channels.get(run_id)
            if channel is None:
                channel = _RunChannel()
                self._channels[run_id] = channel
            return channel

    def publish(
        self,
        *,
        run_id: str,
        thread_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventEnvelope:
        if event_type not in SSE_EVENT_TYPES:
            raise ValueError(f"Unsupported SSE event type: {event_type}")
        sequence = self.repository.max_sequence(run_id) + 1
        envelope = EventEnvelope(
            event_id=new_id(),
            sequence=sequence,
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            timestamp=to_rfc3339(utc_now()) or "",
            payload=payload,
        )
        self.repository.append(
            run_id=run_id,
            event_id=envelope.event_id,
            sequence=sequence,
            event_type=event_type,
            thread_id=thread_id,
            payload=payload,
        )
        if sequence % 50 == 0:
            self.repository.trim(run_id=run_id, keep=self.retention_events)
        channel = self._channel(run_id)
        channel.queue.put(envelope)
        if event_type == "final" or (event_type == "error" and payload.get("terminal") is True):
            channel.queue.put(_END)
            channel.closed = True
        return envelope

    def history(self, *, run_id: str, after_event_id: str | None) -> list[EventEnvelope]:
        try:
            rows = self.repository.list_after(run_id=run_id, after_event_id=after_event_id)
        except KeyError as error:
            raise RunStreamNotFound(after_event_id or "") from error
        return [
            EventEnvelope(
                event_id=event_id,
                sequence=sequence,
                thread_id=thread_id,
                run_id=run_id,
                event_type=event_type,
                timestamp=to_rfc3339(created_at) or "",
                payload=payload,
            )
            for event_id, sequence, event_type, thread_id, payload, created_at in rows
        ]

    def is_stream_closed(self, run_id: str) -> bool:
        with self._lock:
            channel = self._channels.get(run_id)
            return bool(channel and channel.closed)

    def subscribe(self, *, run_id: str) -> _Subscription:
        channel = self._channel(run_id)
        with self._lock:
            channel.subscribers += 1
        return _Subscription(publisher=self, run_id=run_id, channel=channel)

    def _release(self, run_id: str, channel: _RunChannel) -> None:
        with self._lock:
            channel.subscribers -= 1
            if channel.subscribers <= 0 and channel.closed:
                self._channels.pop(run_id, None)

    def close(self, run_id: str) -> None:
        channel = self._channel(run_id)
        channel.queue.put(_END)
        channel.closed = True


class _Subscription:
    """One live SSE listener with a bounded wait so heartbeats stay possible."""

    def __init__(self, *, publisher: RunEventPublisher, run_id: str, channel: _RunChannel) -> None:
        self._publisher = publisher
        self._run_id = run_id
        self._channel = channel
        self._closed = False

    def next(self, timeout: float | None = None) -> EventEnvelope | None:
        """Return the next event, or ``None`` when the wait timed out."""

        if self._closed:
            return None
        try:
            item = self._channel.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _END:
            self.close()
            return None
        return item

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._publisher._release(self._run_id, self._channel)


def payload_timestamp(value: datetime | None) -> str | None:
    return to_rfc3339(value)
