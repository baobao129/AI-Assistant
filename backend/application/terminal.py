"""终态转换的原子记录（HITL 决策与 Run 收尾共用的可靠性边界）。

背景：一个 Run 的终态状态与它的终态事件（``final`` / 带 ``terminal`` 的 ``error``）
分两次写入时，进程可能在「状态已提交、事件尚未发布」之间退出，于是数据库状态与
SSE 事件存储不一致：客户端的事件流永远看不到关闭事件。

因此终态转换遵循：

```text
数据库事务 { Run 终态 CAS  +  终态事件写入（确定性 event_id，幂等） }
提交后 -> 进程内扇出（低延迟优化）
```

- 提交后进程退出也不会丢失终态事件：事件行已随状态一起持久化，
  客户端按 ``Last-Event-ID`` 重连即可从事件存储重放（API-005）。
- 幂等：``event_id`` 由 ``(run_id, event_type, logical_transition)`` 派生，
  同一逻辑终态最多一条事件；状态写入使用 ``required_status`` 条件更新（CAS），
  只有真正完成转换的调用才会写入事件并扇出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.application.events import EventEnvelope, RunEventPublisher
from backend.infrastructure.database import MySQLConnectionPool
from backend.infrastructure.repositories.conversation import RunRepository


@dataclass(frozen=True)
class TerminalTransition:
    """一次终态转换：目标状态 + 它的终态事件。"""

    run_id: str
    thread_id: str
    status: str
    event_type: str
    payload: dict[str, Any]
    # 幂等键的一部分：同一 Run 的同一逻辑终态只允许一条终态事件。
    logical_transition: str
    # 给出时是条件更新（CAS）：只有从该状态转换才生效，重复/并发调用不会重复副作用。
    required_status: str | None = None
    # 执行所有权条件（ADR-035）：只有仍持有该 token 的执行者才能写入该终态。
    # 维护/审批/管理路径不给出它（它们不是某个执行者的提交点）。
    required_owner: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # 终态行同时补写的业务字段（回答正文、快照引用、路由结果等），与既有 update_status 一致。
    final_response: str | None = None
    query_result_id: str | None = None
    routing_outcome: str | None = None
    intent: str | None = None


def apply_terminal_transition(
    cursor,
    *,
    runs: RunRepository,
    events: RunEventPublisher,
    transition: TerminalTransition,
) -> EventEnvelope | None:
    """在调用方事务内完成终态 CAS 与终态事件写入；返回待扇出的信封。

    返回 ``None`` 表示这次调用没有完成转换（状态已被其他调用转为终态），
    调用方不得再扇出事件或执行任何副作用。
    """

    applied = runs.update_status_with_cursor(
        cursor,
        transition.run_id,
        transition.status,
        routing_outcome=transition.routing_outcome,
        intent=transition.intent,
        final_response=transition.final_response,
        query_result_id=transition.query_result_id,
        error_code=transition.error_code,
        error_message=transition.error_message,
        terminal=True,
        required_status=transition.required_status,
        required_owner=transition.required_owner,
    )
    if not applied:
        return None
    prepared = events.prepare_terminal(
        run_id=transition.run_id,
        thread_id=transition.thread_id,
        event_type=transition.event_type,
        payload=transition.payload,
        logical_transition=transition.logical_transition,
    )
    return events.persist(prepared, cursor=cursor)


def record_terminal_transition(
    *,
    pool: MySQLConnectionPool,
    runs: RunRepository,
    events: RunEventPublisher,
    transition: TerminalTransition,
) -> bool:
    """自开事务的便捷入口：终态 CAS 与终态事件在同一事务提交，随后扇出。"""

    with pool.transaction() as cursor:
        envelope = apply_terminal_transition(
            cursor, runs=runs, events=events, transition=transition
        )
    if envelope is None:
        return False
    events.notify(envelope)
    return True
