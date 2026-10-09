"""审批 dispatch 与决策事务的幂等回归测试（Final Review MAJOR 3 / 六-A）。

覆盖两层保护：
1. API 层：``idempotent_replay=True`` 时绝不再次 dispatch successor（API-006）；
2. Application 层：审批 CAS、successor 创建与原始 Run 终态在同一个事务边界内提交；
   只有完成转换的调用才创建 successor，重复/并发 approve 不会创建第二条执行链，
   也不会发布第二个终态 final。

（数据库事务的真实原子性与崩溃回滚由 ``tests/integration/test_hitl_faults_live.py``
用真实 MySQL 验证；此处验证服务的编排与幂等语义。）
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

import backend.application.approvals as approvals_module
from backend.api.routers.approvals import _decide
from backend.api.schemas import ApprovalDecisionRequest
from backend.application.approvals import ApprovalService, DecisionResult
from backend.application.events import EventEnvelope
from backend.domain.auth import AuthenticatedUser
from backend.domain.errors import AppError
from backend.domain.identifiers import utc_now
from backend.infrastructure.repositories.approvals import ApprovalRecord
from backend.infrastructure.repositories.conversation import RunRecord

APPROVAL_ID = "approval-1"
RUN_ID = "run-1"
SUCCESSOR_ID = "run-2"


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="user-1", username="demo", display_name="Demo User")


def _request() -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(conversation_id="conv-1", thread_id="thread-1", run_id=RUN_ID)


class StubState:
    """内存状态：审批行、Run 行与已发布事件。"""

    def __init__(self, approval: ApprovalRecord, original: RunRecord) -> None:
        self.approval = approval
        self.runs: dict[str, RunRecord] = {original.run_id: original}
        self.created: list[str] = []
        self.finals: list[str] = []
        self.persisted: list[str] = []


class StubCursor:
    def execute(self, *args, **kwargs):  # pragma: no cover - 桩函数不直接使用游标
        raise AssertionError("stub cursor must not be used directly")


class StubPool:
    """事务桩：保留提交/回滚语义，避免测试假事务掩盖半完成状态。"""

    @contextmanager
    def transaction(self):
        state = _CURRENT_STATE[-1]
        snapshot = (
            state.approval,
            state.runs.copy(),
            state.created.copy(),
            state.finals.copy(),
            state.persisted.copy(),
        )
        try:
            yield StubCursor()
        except Exception:
            state.approval, runs, created, finals, persisted = snapshot
            state.runs = runs
            state.created = created
            state.finals = finals
            state.persisted = persisted
            raise


class StubApprovals:
    """只读桩：决策写入由被 monkeypatch 的模块级 CAS 函数负责。"""

    def __init__(self, state: StubState) -> None:
        self.state = state

    def get(self, approval_request_id: str) -> ApprovalRecord | None:
        record = self.state.approval
        return record if approval_request_id == record.approval_request_id else None

    def decide(self, approval_request_id: str, *, lifecycle, decided_by, successor_run_id=None):
        return approvals_module.decide_with_cursor(
            StubCursor(),
            approval_request_id,
            lifecycle=lifecycle,
            decided_by=decided_by,
            successor_run_id=successor_run_id,
        )


class StubRuns:
    def __init__(self, state: StubState) -> None:
        self.state = state

    def get(self, run_id: str) -> RunRecord | None:
        return self.state.runs.get(run_id)

    def create(self, **kwargs) -> RunRecord:
        raise AssertionError("事务路径不应直接调用 RunRepository.create")

    def update_status_with_cursor(self, cursor, run_id: str, status: str, **kwargs) -> bool:
        current = self.state.runs.get(run_id)
        if current is None:
            return False
        required = kwargs.get("required_status")
        if required is not None and current.status != required:
            return False
        self.state.runs[run_id] = replace(current, status=status)
        return True

    def update_status(self, run_id: str, status: str, **kwargs) -> None:
        current = self.state.runs.get(run_id)
        if current is not None:
            self.state.runs[run_id] = replace(current, status=status)


class StubEvents:
    """事件发布器桩：终态事件按「准备 -> 事务内持久化 -> 提交后扇出」三段式记录。"""

    def __init__(self, state: StubState) -> None:
        self.state = state

    def prepare_terminal(
        self, *, run_id, thread_id, event_type, payload, logical_transition
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=f"{run_id}:{event_type}:{logical_transition}",
            sequence=0,
            thread_id=thread_id,
            run_id=run_id,
            event_type=event_type,
            timestamp="",
            payload=payload,
        )

    def persist(self, envelope: EventEnvelope, *, cursor) -> EventEnvelope:
        self.state.persisted.append(envelope.event_id)
        return envelope

    def notify(self, envelope: EventEnvelope) -> None:
        self.state.finals.append(envelope.run_id)

    def publish(self, *, run_id: str, thread_id: str, event_type: str, payload: dict) -> None:
        if event_type in {"final", "error"}:
            self.state.finals.append(run_id)


class StubContainer:
    def __init__(self, approvals: ApprovalService) -> None:
        self.approvals = approvals


class StubDispatcher:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.dispatched: list[tuple[str, str | None]] = []

    def dispatch(self, run_id: str, *, approval_request_id: str | None = None) -> bool:
        if not self.available:
            return False
        self.dispatched.append((run_id, approval_request_id))
        return True


def _approval_record(*, status: str = "pending") -> ApprovalRecord:
    now = utc_now()
    return ApprovalRecord(
        approval_request_id=APPROVAL_ID,
        run_id=RUN_ID,
        thread_id="thread-1",
        conversation_id="conv-1",
        user_id="user-1",
        status=status,
        summary={"row_count": 3, "columns": ["order_id"], "format": "csv"},
        binding={"query_result_id": "query-result-1", "format": "csv"},
        created_at=now,
        expires_at=now.replace(year=now.year + 1),
        decided_at=None,
        decided_by=None,
        successor_run_id=None,
        artifact_id=None,
        side_effect_completed_at=None,
    )


def _original_run() -> RunRecord:
    now = utc_now()
    return RunRecord(
        run_id=RUN_ID,
        conversation_id="conv-1",
        thread_id="thread-1",
        user_id="user-1",
        user_input="export sales amount data to csv",
        status="awaiting_approval",
        routing_outcome="routed",
        intent="structured_data",
        final_response=None,
        error_code=None,
        error_message=None,
        approval_request_id=APPROVAL_ID,
        original_run_id=None,
        predecessor_run_id=None,
        query_result_id=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


_CURRENT_STATE: list[StubState] = []


@pytest.fixture(autouse=True)
def _in_memory_transaction(monkeypatch):
    """把事务内的两个模块级写入映射到内存状态，保持与生产相同的语义。"""

    def fake_decide_with_cursor(
        cursor, approval_request_id, *, lifecycle, decided_by, successor_run_id=None
    ):
        state = _CURRENT_STATE[-1]
        if approval_request_id != state.approval.approval_request_id:
            return False
        if state.approval.status != "pending":
            return False
        state.approval = replace(
            state.approval,
            status=lifecycle,
            decided_by=decided_by,
            successor_run_id=successor_run_id,
        )
        return True

    def fake_insert_run_row(cursor, *, run_id, **kwargs):
        state = _CURRENT_STATE[-1]
        allowed = {
            "conversation_id",
            "thread_id",
            "user_id",
            "user_input",
            "status",
            "predecessor_run_id",
            "original_run_id",
            "approval_request_id",
        }
        run = replace(
            _original_run(),
            run_id=run_id,
            **{key: value for key, value in kwargs.items() if key in allowed},
        )
        state.runs[run.run_id] = run
        state.created.append(run.run_id)
        return run

    monkeypatch.setattr(approvals_module, "decide_with_cursor", fake_decide_with_cursor)
    monkeypatch.setattr(approvals_module, "insert_run_row", fake_insert_run_row)


def _service(*, status: str = "pending") -> tuple[ApprovalService, StubState]:
    state = StubState(_approval_record(status=status), _original_run())
    _CURRENT_STATE.clear()
    _CURRENT_STATE.append(state)
    service = ApprovalService(
        approvals=StubApprovals(state),
        runs=StubRuns(state),
        threads=None,
        events=StubEvents(state),
        pool=StubPool(),
    )
    return service, state


# API 层 ----------------------------------------------------------------


def test_first_approve_dispatches_the_successor_exactly_once():
    service, state = _service()
    dispatcher = StubDispatcher()

    response = _decide(
        APPROVAL_ID, _request(), StubContainer(service), dispatcher, _user(), "approve"
    )

    assert len(state.created) == 1
    successor = state.created[0]
    assert dispatcher.dispatched == [(successor, APPROVAL_ID)]
    assert response.successor_run_id == successor
    assert response.stream_url == f"/api/v1/runs/{successor}/events"


def test_replayed_approve_returns_same_successor_without_dispatching_again():
    # idempotent_replay 时 API 不得再次 dispatch，但仍返回同一逻辑结果。
    result = DecisionResult(
        APPROVAL_ID,
        "approved",
        RUN_ID,
        "resume_scheduled",
        SUCCESSOR_ID,
        idempotent_replay=True,
    )

    class ReplayApprovals:
        def decide(self, *args, **kwargs) -> DecisionResult:
            return result

    dispatcher = StubDispatcher()

    response = _decide(
        APPROVAL_ID, _request(), StubContainer(ReplayApprovals()), dispatcher, _user(), "approve"
    )

    assert dispatcher.dispatched == []
    assert response.successor_run_id == SUCCESSOR_ID
    assert response.stream_url == f"/api/v1/runs/{SUCCESSOR_ID}/events"


def test_reject_never_dispatches_a_successor():
    service, state = _service()
    dispatcher = StubDispatcher()

    response = _decide(
        APPROVAL_ID, _request(), StubContainer(service), dispatcher, _user(), "reject"
    )

    assert dispatcher.dispatched == []
    assert response.successor_run_id is None
    assert response.stream_url is None
    assert state.created == []


def test_capacity_exhausted_approve_still_persists_the_successor_intent():
    # 六-B：调度容量耗尽时 approve 仍返回可追踪的 202 与 successor，调度意图已持久化；
    # 恢复任务会在之后的维护周期重新 dispatch（见 live 恢复测试）。
    service, state = _service()
    dispatcher = StubDispatcher(available=False)

    response = _decide(
        APPROVAL_ID, _request(), StubContainer(service), dispatcher, _user(), "approve"
    )

    assert dispatcher.dispatched == []
    assert len(state.created) == 1
    successor = state.created[0]
    assert state.runs[successor].status == "accepted"
    assert response.successor_run_id == successor
    assert response.stream_url == f"/api/v1/runs/{successor}/events"


# Application 层 --------------------------------------------------------


def test_only_the_transition_winner_creates_one_successor():
    service, state = _service()

    def decide():
        return service.decide(
            APPROVAL_ID,
            decision="approve",
            user_id="user-1",
            conversation_id="conv-1",
            thread_id="thread-1",
            run_id=RUN_ID,
        )

    first = decide()
    second = decide()

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert len(state.created) == 1
    assert first.successor_run_id == second.successor_run_id == state.created[0]
    assert state.runs[RUN_ID].status == "handoff_complete"
    assert state.finals == [RUN_ID]


def test_approve_after_reject_is_conflict():
    service, _ = _service(status="rejected")

    with pytest.raises(AppError):
        service.decide(
            APPROVAL_ID,
            decision="approve",
            user_id="user-1",
            conversation_id="conv-1",
            thread_id="thread-1",
            run_id=RUN_ID,
        )


def test_repeated_reject_does_not_publish_a_second_final():
    service, state = _service()

    def reject():
        return service.decide(
            APPROVAL_ID,
            decision="reject",
            user_id="user-1",
            conversation_id="conv-1",
            thread_id="thread-1",
            run_id=RUN_ID,
        )

    first = reject()
    second = reject()

    assert first.successor_run_id is None and second.successor_run_id is None
    assert state.created == []
    assert state.finals == [RUN_ID]
    assert state.runs[RUN_ID].status == "rejected"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_terminal_cas_failure_rolls_back_approval_and_successor(monkeypatch, decision):
    """终态 CAS 失败时，审批决策和 successor 都必须随事务回滚。"""

    service, state = _service()
    monkeypatch.setattr(approvals_module, "apply_terminal_transition", lambda *args, **kwargs: None)

    with pytest.raises(AppError):
        service.decide(
            APPROVAL_ID,
            decision=decision,
            user_id="user-1",
            conversation_id="conv-1",
            thread_id="thread-1",
            run_id=RUN_ID,
        )

    assert state.approval.status == "pending"
    assert state.approval.successor_run_id is None
    assert state.runs[RUN_ID].status == "awaiting_approval"
    assert state.created == []
    assert state.finals == []
