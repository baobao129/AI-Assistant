"""SSE Application Event 的投递与重放（API-005）。

事件是传输/可观测性记录。它们绝不修改 Graph State，且重放存储是
Run Event 存储，而非 Conversation Persistence。
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
    """对一个其流不可用的 Run 请求了重放。"""


class RunEventPublisher:
    """分配每个 Run 的序列号、持久化以供重放，并扇出给实时监听者。"""

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
        event_id = new_id()
        # 存储层原子地预留每个 Run 的序列号；返回的序列号是权威值，
        # 且已为本事件持久化。
        sequence = self.repository.append(
            run_id=run_id,
            event_id=event_id,
            event_type=event_type,
            thread_id=thread_id,
            payload=payload,
        )
        envelope = EventEnvelope(
            event_id=event_id,
            sequence=sequence,
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            timestamp=to_rfc3339(utc_now()) or "",
            payload=payload,
        )
        # 周期性按保留上限裁剪该 Run 的事件存储，避免其无限增长。
        if sequence % 50 == 0:
            self.repository.trim(run_id=run_id, keep=self.retention_events)
        channel = self._channel(run_id)
        channel.queue.put(envelope)
        # 终态事件（final，或带 terminal 标记的 error）之后关闭该 Run 的实时流。
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
    """单个实时 SSE 监听者，采用有界等待以便仍能发送心跳。"""

    def __init__(self, *, publisher: RunEventPublisher, run_id: str, channel: _RunChannel) -> None:
        self._publisher = publisher
        self._run_id = run_id
        self._channel = channel
        self._closed = False

    def next(self, timeout: float | None = None) -> EventEnvelope | None:
        """返回下一个事件；等待超时则返回 ``None``。"""

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
