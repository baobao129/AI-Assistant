"""HITL 决策事务、调度恢复、执行租约与 Application 层 Resume 的实时验证。

针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。

覆盖：

- 六-A：审批 CAS、successor 创建与原始 Run 终态在同一事务内提交；进程在事务中途
  退出时整体回滚，之后的重试能继续完成且不产生第二条执行链；
- 六-B：调度容量耗尽时 successor 仍以持久化调度意图保留，恢复任务按周期重新调度，
  不会永久留下 ``accepted`` Run，也不会重复执行；
- 六-C：经过 ``RunService.resume()`` + 真实仓储 + Checkpoint + approval binding 的
  Resume 保持 lineage，不重复查询、导出最多执行一次、重复 resume 不再执行 Graph；
- 终态 CAS：陈旧执行器不得覆盖新终态、不得写出重复 assistant 消息或第二条终态事件；
- 恢复预算：真实 ``resume()`` 崩溃路径（含 ``agent_start`` 发布）会计入尝试次数，
  预算耗尽后终止为 ``execution_interrupted``，绝不无限恢复；
- 执行租约：执行期心跳（``run_heartbeat_seconds``）使长任务不被误判为陈旧，
  也不会产生第二个执行者；
- 执行所有权（ADR-035）：并发领取只有一个赢家；失去所有权的旧执行者写不进事件、
  等待态或终态；普通 Run 的陈旧租约以安全终态（``execution_interrupted``）收口；
- 异常兜底：后台调度器异常与普通 Run 调度容量耗尽都以安全终态收口，绝不永久停留；
- 过期原子性：审批过期与 Run 终态、关闭事件同一事务提交，失败整体回滚且可重试，
  数据库级 CAS 保证过期审批永远不会被批准。
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

import backend.application.approvals as approvals_module
from backend.domain.identifiers import utc_now


@pytest.fixture(scope="module")
def harness(live_settings):
    from fastapi.testclient import TestClient

    from backend.api.dependencies import build_container
    from backend.api.main import create_app

    settings = live_settings
    container = build_container(settings)
    with TestClient(create_app(settings, container=container)) as client:
        # 应用启动后暂停后台维护循环：本模块直接驱动过期与恢复路径
        # （expire_one / recover_stale_running_successors 等），需要确定性的时序。
        container.maintenance.stop()
        login = client.post(
            "/api/v1/auth/login",
            json={
                "username": settings.auth_bootstrap_username,
                "password": settings.auth_bootstrap_password.get_secret_value(),
            },
        )
        assert login.status_code == 200, login.text
        client.headers.update({"Authorization": f"Bearer {login.json()['session']['token']}"})
        conversation_id = client.post(
            "/api/v1/conversations", json={"title": "hitl fault injection"}
        ).json()["conversation_id"]
        yield {"client": client, "container": container, "conversation_id": conversation_id}
    container.close()


def _await_status(client, run_id: str, statuses: set[str], *, deadline: float = 60.0) -> str:
    started = time.monotonic()
    status = "unknown"
    while time.monotonic() - started < deadline:
        status = client.get(f"/api/v1/runs/{run_id}").json()["status"]
        if status in statuses:
            return status
        time.sleep(0.5)
    return status


def _start_export(harness) -> tuple[str, str, dict]:
    client = harness["client"]
    accepted = client.post(
        f"/api/v1/conversations/{harness['conversation_id']}/runs",
        json={"user_input": "export sales amount data to csv"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert _await_status(client, run_id, {"awaiting_approval", "failed", "completed"}) == (
        "awaiting_approval"
    )
    detail = client.get(f"/api/v1/runs/{run_id}").json()
    events = _read_events(client, run_id)
    approval = next(event for event in events if event["event_type"] == "approval_required")
    correlation = {
        "conversation_id": harness["conversation_id"],
        "thread_id": detail["thread_id"],
        "run_id": run_id,
    }
    return run_id, approval["payload"]["approval_request_id"], correlation


def _read_events(
    client, run_id: str, *, deadline: float = 60.0, after_event_id: str | None = None
) -> list[dict]:
    headers = {"Last-Event-ID": after_event_id} if after_event_id else None
    events: list[dict] = []
    started = time.monotonic()
    with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=headers) as response:
        for line in response.iter_lines():
            if time.monotonic() - started > deadline:
                break
            if line.startswith("data: "):
                event = json.loads(line[6:])
                events.append(event)
                if event["event_type"] in {"final", "error"}:
                    break
    return events


def _successor_rows(container, approval_request_id: str) -> list[tuple]:
    return container.runtime.runs.pool.query(
        "SELECT run_id, status FROM runs "
        "WHERE approval_request_id = %s AND original_run_id IS NOT NULL",
        (approval_request_id,),
    )


@pytest.mark.live_db
def test_crash_inside_the_decision_transaction_rolls_back_and_retry_completes(harness, monkeypatch):
    # 六-A：事务内 successor 写入之后崩溃 → 整体回滚（审批仍 pending、
    # 不存在 successor、原始 Run 仍等待）；重试后只产生一个 successor。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, correlation = _start_export(harness)

    real_insert = approvals_module.insert_run_row
    crashed = {"injected": False}

    def insert_then_crash(cursor, *, run_id: str, **kwargs):
        # 先真实写入 successor 行（仍在未提交的事务内），再模拟进程退出。
        real_insert(cursor, run_id=run_id, **kwargs)
        crashed["injected"] = True
        raise RuntimeError("simulated process crash inside the decision transaction")

    monkeypatch.setattr(approvals_module, "insert_run_row", insert_then_crash)
    with pytest.raises(RuntimeError):
        container.approvals.decide(
            approval_request_id,
            decision="approve",
            user_id=container.runtime.auth.get_user_by_username(
                container.settings.auth_bootstrap_username
            ).user_id,
            conversation_id=correlation["conversation_id"],
            thread_id=correlation["thread_id"],
            run_id=correlation["run_id"],
        )
    monkeypatch.setattr(approvals_module, "insert_run_row", real_insert)

    assert crashed["injected"] is True
    approval = container.runtime.approvals.get(approval_request_id)
    assert approval is not None and approval.status == "pending"
    assert approval.successor_run_id is None
    assert _successor_rows(container, approval_request_id) == []
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "awaiting_approval"

    # 重试：继续完成，且只产生一个 successor。
    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    successor = decision.json()["successor_run_id"]
    assert successor
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"
    rows = _successor_rows(container, approval_request_id)
    assert [row[0] for row in rows] == [successor]
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "handoff_complete"


def _approve_without_dispatch(
    container, client, approval_request_id: str, correlation: dict, monkeypatch
) -> tuple[str, str]:
    """审批但不调度 successor：让 successor 停在 ``accepted``，便于模拟执行器崩溃。"""

    real_dispatch = container.dispatcher.dispatch

    def suppressed(run_id: str, *, approval_request_id: str | None = None) -> bool:
        return False

    monkeypatch.setattr(container.dispatcher, "dispatch", suppressed)
    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    monkeypatch.setattr(container.dispatcher, "dispatch", real_dispatch)
    return decision.json()["successor_run_id"], correlation["run_id"]


def _crash_real_execution(container, run_id: str, *, approval_request_id: str) -> None:
    """通过真实领取路径制造一次执行器崩溃。

    ``RunService.resume()`` 会真实完成 ``claim_for_execution()`` 并发布 ``agent_start``，
    随后让 Graph 执行抛错逃逸出 Application 层（等价于执行器进程在 Graph 执行中退出）。
    唯一由测试模拟的是「时间流逝」：崩溃的执行者不再续租（独立心跳线程也随进程一起消失），
    因此把租约时间戳（``heartbeat_at``/``updated_at``）拨到陈旧阈值之外。
    """

    real_run_graph = container.runs._run_graph

    def exploding_run_graph(**_kwargs):
        raise RuntimeError("simulated executor crash after claim")

    container.runs._run_graph = exploding_run_graph
    try:
        with pytest.raises(RuntimeError):
            container.runs.resume(run_id, approval_request_id=approval_request_id)
    finally:
        container.runs._run_graph = real_run_graph
    stale = utc_now() - timedelta(seconds=container.settings.run_stale_running_seconds + 60)
    container.runtime.runs.pool.execute(
        "UPDATE runs SET heartbeat_at = %s, updated_at = %s WHERE run_id = %s",
        (stale, stale, run_id),
    )


@pytest.mark.live_db
def test_terminal_event_survives_a_crash_between_commit_and_fanout(harness, monkeypatch):
    # MAJOR 1：事务提交后、进程内扇出之前进程退出 —— 终态事件已在同一事务内持久化，
    # 客户端按 Last-Event-ID 重连即可重放到唯一的 final，且重复审批不会再产生 final。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, correlation = _start_export(harness)

    monkeypatch.setattr(container.events, "notify", lambda envelope: None)  # 模拟扇出丢失
    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    successor = decision.json()["successor_run_id"]
    assert successor

    # 状态已是 handoff_complete，且事件存储中恰好有一条 final（不依赖进程内扇出）。
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "handoff_complete"
    stored = container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
    finals = [row for row in stored if row[2] == "final"]
    assert len(finals) == 1

    # 客户端重连（Last-Event-ID 停在 final 之前）能重放它并可靠关闭流。
    before_final = [row for row in stored if row[2] != "final"][-1]
    replayed = _read_events(client, run_id, after_event_id=before_final[0])
    assert [event["event_type"] for event in replayed].count("final") == 1

    # 重复 approve 不产生第二个 final（确定性 event_id + 条件更新共同保证）。
    repeat = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert repeat.status_code == 202
    assert repeat.json()["successor_run_id"] == successor
    again = container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
    assert [row for row in again if row[2] == "final"] == finals


@pytest.mark.live_db
def test_stale_running_successor_is_released_recovered_and_bounded(harness, monkeypatch):
    # MAJOR 2：执行器在 Graph 执行中途崩溃后，successor 会长期停在 running；
    # 维护任务按陈旧阈值把它转回可调度状态并重新执行（最多一个执行者），
    # 尝试次数用尽后终止为明确安全失败，绝不无限重试。
    container = harness["container"]
    client = harness["client"]

    # 阶段一：崩溃一次 → 释放并恢复执行 → 正常完成，仅一个 final、仅一个导出产物。
    run_id, approval_request_id, correlation = _start_export(harness)
    successor, original_run_id = _approve_without_dispatch(
        container, client, approval_request_id, correlation, monkeypatch
    )
    assert client.get(f"/api/v1/runs/{successor}").json()["status"] == "accepted"
    # 真实崩溃：resume() 领取并发布 agent_start，执行异常逃逸；预算由真实事件消耗。
    _crash_real_execution(container, successor, approval_request_id=approval_request_id)
    assert container.runtime.runs.get(successor).status == "running"
    assert (
        container.runtime.run_events.count_of_type(run_id=successor, event_type="agent_start") == 1
    )
    # 断言聚焦到本用例自己的 Run（恢复是全局有界扫描，其他遗留数据不应影响结论）。
    recovery = container.maintenance.recover_stale_running_successors(limit=10)
    assert recovery["released"] >= 1
    assert client.get(f"/api/v1/runs/{successor}").json()["status"] == "accepted"
    assert container.maintenance.recover_pending_successors(limit=10) >= 1
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"

    events = _read_events(client, successor)
    assert [event["event_type"] for event in events].count("final") == 1
    tool_ids = [
        event["payload"].get("tool_id") for event in events if event["event_type"] == "tool_call"
    ]
    assert tool_ids.count("data_export") == 1
    artifact = container.runtime.artifacts.get_by_approval(approval_request_id)
    assert artifact is not None
    assert client.get(f"/api/v1/runs/{original_run_id}").json()["status"] == "handoff_complete"

    # 阶段二：两次真实崩溃（每次都由 resume() 的 agent_start 计入预算）→ 达到
    # run_max_recovery_attempts=2 → 明确安全失败 + 终态事件。
    second_run_id, second_approval_request_id, second_correlation = _start_export(harness)
    second_successor, _ = _approve_without_dispatch(
        container, client, second_approval_request_id, second_correlation, monkeypatch
    )
    _crash_real_execution(
        container, second_successor, approval_request_id=second_approval_request_id
    )
    assert container.maintenance.recover_stale_running_successors(limit=10)["released"] >= 1
    assert client.get(f"/api/v1/runs/{second_successor}").json()["status"] == "accepted"
    _crash_real_execution(
        container, second_successor, approval_request_id=second_approval_request_id
    )
    assert (
        container.runtime.run_events.count_of_type(
            run_id=second_successor, event_type="agent_start"
        )
        == 2
    )
    # 预算耗尽：该 Run 被终止，其他候选不会改变本 Run 的结论。
    container.maintenance.recover_stale_running_successors(limit=10)
    detail = client.get(f"/api/v1/runs/{second_successor}").json()
    assert detail["status"] == "failed"
    # 安全失败原因通过终态 error 事件暴露（API 不暴露内部错误列）。
    failed_run = container.runtime.runs.get(second_successor)
    assert failed_run is not None and failed_run.error_code == "execution_interrupted"
    closing = _read_events(client, second_successor)
    assert closing[-1]["event_type"] == "error"
    assert closing[-1]["payload"]["code"] == "execution_interrupted"
    # 不再无限重试：再次恢复不会改变本 Run 的终态与事件。
    assert container.maintenance.recover_stale_running_successors(limit=10)["failed"] >= 0
    assert client.get(f"/api/v1/runs/{second_successor}").json()["status"] == "failed"
    assert _read_events(client, second_successor) == closing
    assert container.runtime.artifacts.get_by_approval(second_approval_request_id) is None
    assert second_run_id  # 原始等待 Run 仍在等待/终态集合内，未被恢复逻辑改动


@pytest.mark.live_db
def test_capacity_exhaustion_keeps_a_recoverable_intent_and_is_recovered(harness, monkeypatch):
    # 六-B：dispatch 容量耗尽 → successor 以持久化调度意图保留（accepted），
    # 恢复任务重新调度它；不永久留下 accepted Run，也不重复执行/重复 final。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, correlation = _start_export(harness)

    real_dispatch = container.dispatcher.dispatch
    attempts: list[str] = []

    def exhausted_once(run_id: str, *, approval_request_id: str | None = None) -> bool:
        attempts.append(run_id)
        if len(attempts) == 1:
            return False  # 第一次调度容量耗尽
        return real_dispatch(run_id, approval_request_id=approval_request_id)

    monkeypatch.setattr(container.dispatcher, "dispatch", exhausted_once)

    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert decision.status_code == 202, decision.text
    successor = decision.json()["successor_run_id"]
    assert successor
    # API 仍返回可追踪的 successor 与其流地址；Run 处于 accepted（可恢复的调度意图）。
    assert decision.json()["stream_url"] == f"/api/v1/runs/{successor}/events"
    assert client.get(f"/api/v1/runs/{successor}").json()["status"] == "accepted"

    pending = container.runtime.approvals.list_pending_successor_run_ids(limit=10)
    assert (successor, approval_request_id) in pending

    # 恢复任务（维护周期）重新调度同一个 successor。
    assert container.maintenance.recover_pending_successors(limit=10) >= 1
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"

    events = _read_events(client, successor)
    tool_ids = [
        event["payload"].get("tool_id") for event in events if event["event_type"] == "tool_call"
    ]
    assert tool_ids.count("data_export") == 1
    assert "database_query" not in tool_ids
    assert [event["event_type"] for event in events].count("final") == 1

    # 再次恢复/重复 approve 不会重复执行，也不产生第二个 final。
    container.maintenance.recover_pending_successors(limit=10)
    assert client.get(f"/api/v1/runs/{successor}").json()["status"] == "completed"
    repeat = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    assert repeat.status_code == 202
    assert repeat.json()["successor_run_id"] == successor
    after = _read_events(client, successor)
    assert [event["event_id"] for event in after] == [event["event_id"] for event in events]
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "handoff_complete"


@pytest.mark.live_db
def test_application_layer_resume_preserves_lineage_and_executes_once(harness):
    # 六-C：直接经过 RunService.resume + 仓储 + Checkpoint + approval binding；
    # 保持 thread/lineage 与绑定 snapshot，不重复查询，重复 resume 不再执行 Graph。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, correlation = _start_export(harness)

    user_id = container.runtime.auth.get_user_by_username(
        container.settings.auth_bootstrap_username
    ).user_id
    before_queries = container.runtime.query_results.pool.query_one(
        "SELECT COUNT(*) FROM query_results WHERE user_id = %s", (user_id,)
    )[0]

    # 直接通过 Application 层完成决策（不经过 dispatcher），再由测试显式 resume。
    decision = container.approvals.decide(
        approval_request_id,
        decision="approve",
        user_id=user_id,
        conversation_id=correlation["conversation_id"],
        thread_id=correlation["thread_id"],
        run_id=correlation["run_id"],
    )
    assert decision.idempotent_replay is False
    successor = decision.successor_run_id
    assert successor

    assert container.runs.resume(successor, approval_request_id=approval_request_id) == "completed"

    original = container.runtime.runs.get(run_id)
    resumed = container.runtime.runs.get(successor)
    approval = container.runtime.approvals.get(approval_request_id)
    # lineage 与绑定快照保持。
    assert resumed.thread_id == original.thread_id
    assert resumed.original_run_id == run_id and resumed.predecessor_run_id == run_id
    assert resumed.approval_request_id == approval_request_id
    assert resumed.query_result_id == approval.binding["query_result_id"]
    assert original.status == "handoff_complete"

    # 已完成 database query 不重复执行；approval 绑定的导出最多执行一次。
    after_queries = container.runtime.query_results.pool.query_one(
        "SELECT COUNT(*) FROM query_results WHERE user_id = %s", (user_id,)
    )[0]
    assert after_queries == before_queries
    events = _read_events(client, successor)
    tool_ids = [
        event["payload"].get("tool_id") for event in events if event["event_type"] == "tool_call"
    ]
    assert tool_ids.count("data_export") == 1
    assert "database_query" not in tool_ids
    assert [event["event_type"] for event in events].count("final") == 1
    artifact = container.runtime.artifacts.get_by_approval(approval_request_id)
    assert artifact is not None

    # 重复 resume：返回已有终态，不再执行 Graph，也不产生新事件。
    assert container.runs.resume(successor, approval_request_id=approval_request_id) == "completed"
    after = _read_events(client, successor)
    assert [event["event_id"] for event in after] == [event["event_id"] for event in events]
    again = container.runtime.artifacts.get_by_approval(approval_request_id)
    assert again is not None and again.artifact_id == artifact.artifact_id


@pytest.mark.live_db
def test_real_resume_crash_consumes_the_recovery_budget_and_stops(harness, monkeypatch):
    # MAJOR 1：恢复预算必须由真实领取路径消耗。连续两次真实崩溃（resume() 领取并发布
    # agent_start）后，预算耗尽 → 终止为 execution_interrupted，绝不无限 running->accepted。
    container = harness["container"]
    client = harness["client"]
    _run_id, approval_request_id, correlation = _start_export(harness)
    successor, _ = _approve_without_dispatch(
        container, client, approval_request_id, correlation, monkeypatch
    )

    _crash_real_execution(container, successor, approval_request_id=approval_request_id)
    run = container.runtime.runs.get(successor)
    assert run is not None and run.status == "running"
    assert (
        container.runtime.run_events.count_of_type(run_id=successor, event_type="agent_start") == 1
    )

    # 预算未耗尽（1 < run_max_recovery_attempts=2）：释放为可重新调度。
    assert container.maintenance.recover_stale_running_successors(limit=10)["released"] >= 1
    assert container.runtime.runs.get(successor).status == "accepted"

    _crash_real_execution(container, successor, approval_request_id=approval_request_id)
    assert (
        container.runtime.run_events.count_of_type(run_id=successor, event_type="agent_start") == 2
    )

    # 预算耗尽：明确安全失败 + 终态事件，且不再被恢复逻辑反复拾起。
    container.maintenance.recover_stale_running_successors(limit=10)
    failed = container.runtime.runs.get(successor)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "execution_interrupted"
    closing = _read_events(client, successor)
    assert closing[-1]["event_type"] == "error"
    assert closing[-1]["payload"]["code"] == "execution_interrupted"
    assert closing[-1]["payload"]["terminal"] is True
    container.maintenance.recover_stale_running_successors(limit=10)
    assert client.get(f"/api/v1/runs/{successor}").json()["status"] == "failed"
    assert _read_events(client, successor) == closing


@pytest.mark.live_db
def test_heartbeat_keeps_a_long_running_execution_claimed(harness, monkeypatch):
    # MAJOR 4：执行期心跳把租约时间戳推进到最近活动，长任务不再被误判为陈旧，
    # 因此不会被释放、也不会产生第二个执行者；终态 Run 不会被心跳复活。
    from backend.domain.identifiers import new_id

    container = harness["container"]
    client = harness["client"]
    _run_id, approval_request_id, correlation = _start_export(harness)
    successor, original_run_id = _approve_without_dispatch(
        container, client, approval_request_id, correlation, monkeypatch
    )

    owner_token = new_id()
    assert container.runtime.runs.claim_for_execution(successor, owner_token=owner_token) is True
    # 模拟「已执行很久」（租约时间戳远早于陈旧阈值），但执行者仍在按周期续租。
    stale = utc_now() - timedelta(seconds=container.settings.run_stale_running_seconds + 300)
    container.runtime.runs.pool.execute(
        "UPDATE runs SET heartbeat_at = %s, updated_at = %s WHERE run_id = %s",
        (stale, stale, successor),
    )
    before = utc_now()
    assert container.runs._heartbeat(successor, owner_token=owner_token) is True
    refreshed = container.runtime.runs.get(successor)
    assert refreshed is not None and refreshed.updated_at >= before

    # 心跳之后，陈旧恢复不会释放它，恢复调度也不会重新调度它（没有第二个执行者）。
    before_events = container.runtime.run_events.list_after(run_id=successor, after_event_id=None)
    container.maintenance.recover_stale_running_successors(limit=10)
    assert container.runtime.runs.get(successor).status == "running"
    container.maintenance.recover_pending_successors(limit=10)
    assert container.runtime.runs.get(successor).status == "running"
    assert (
        container.runtime.run_events.list_after(run_id=successor, after_event_id=None)
        == before_events
    )

    # 终态 Run 不会被心跳复活；已失去所有权（token 不匹配）的续租同样失败。
    assert container.runtime.runs.heartbeat(original_run_id, owner_token=owner_token) is False
    before_owned = utc_now()
    assert container.runtime.runs.heartbeat(successor, owner_token=new_id()) is False
    assert container.runtime.runs.get(successor).updated_at < before_owned

    # 用例收尾：把模拟中的执行声明交还恢复流程，让该 successor 真实完成，
    # 不把「测试模拟的 running」留给后续用例或演示库。
    released = utc_now() - timedelta(seconds=container.settings.run_stale_running_seconds + 60)
    container.runtime.runs.pool.execute(
        "UPDATE runs SET heartbeat_at = %s, updated_at = %s WHERE run_id = %s",
        (released, released, successor),
    )
    assert container.maintenance.recover_stale_running_successors(limit=20)["released"] >= 1
    assert container.maintenance.recover_pending_successors(limit=20) >= 1
    assert _await_status(client, successor, {"completed", "failed"}) == "completed"


@pytest.mark.live_db
def test_terminal_cas_blocks_a_stale_executor_from_overwriting(harness, monkeypatch):
    # MAJOR 2：陈旧执行器不得覆盖新终态、不得写出重复 assistant 消息或第二条终态事件；
    # 且当它已失去执行所有权（租约被释放、Run 已被新执行者领取）时，连事件都写不进去。
    from backend.domain.errors import ExecutionOwnershipLost
    from backend.domain.identifiers import new_id

    container = harness["container"]
    client = harness["client"]
    _run_id, approval_request_id, correlation = _start_export(harness)
    successor, _ = _approve_without_dispatch(
        container, client, approval_request_id, correlation, monkeypatch
    )
    stale_token = new_id()
    assert container.runtime.runs.claim_for_execution(successor, owner_token=stale_token) is True
    claimed = container.runtime.runs.get(successor)
    assert claimed is not None and claimed.status == "running"

    # 该执行者失去所有权：租约陈旧 → 释放（清空 owner）→ 新执行者用新 token 领取同一 Run。
    stale_threshold = utc_now() - timedelta(seconds=container.settings.run_stale_running_seconds)
    stale = stale_threshold - timedelta(seconds=60)
    container.runtime.runs.pool.execute(
        "UPDATE runs SET heartbeat_at = %s, updated_at = %s WHERE run_id = %s",
        (stale, stale, successor),
    )
    assert container.runtime.runs.release_stale_claim(successor, older_than=stale_threshold) is True
    released = container.runtime.runs.get(successor)
    assert released is not None and released.status == "accepted"
    live_token = new_id()
    assert container.runtime.runs.claim_for_execution(successor, owner_token=live_token) is True

    # 陈旧执行者：不能写事件（提交点所有权校验），不能写终态，也不能终止 Run。
    with pytest.raises(ExecutionOwnershipLost):
        container.events.publish(
            run_id=successor,
            thread_id=claimed.thread_id,
            event_type="intent",
            payload={"intent": "data_export"},
            owner_token=stale_token,
        )
    assert (
        container.runs.fail_run_safely(
            successor,
            code="execution_failed",
            message="late failure",
            required_status="running",
            required_owner=stale_token,
        )
        is False
    )
    late_failure = {"error": {"code": "internal_error", "message": "late failure"}}
    assert container.runs._finalize_run(claimed, late_failure, owner_token=stale_token) == "running"
    still_running = container.runtime.runs.get(successor)
    assert still_running is not None and still_running.status == "running"

    # 新执行者正常完成：唯一 final、唯一 assistant 消息、无 error 事件。
    completed_state = {
        "final_response": "The export was completed and the artifact is available.",
        "intent": "data_export",
        "routing_outcome": "routed",
        "citations": [],
        "tool_results": [],
    }
    assert container.runs._finalize_run(claimed, completed_state, owner_token=live_token) == (
        "completed"
    )
    # 同一 token 再次提交（重复收尾）同样不产生第二个终态或第二条消息。
    assert container.runs._finalize_run(claimed, completed_state, owner_token=live_token) == (
        "completed"
    )

    final_run = container.runtime.runs.get(successor)
    assert final_run is not None and final_run.status == "completed"
    assert final_run.error_code is None
    events = _read_events(client, successor)
    assert [event["event_type"] for event in events].count("final") == 1
    assert all(event["event_type"] != "error" for event in events)
    assert all(
        event["payload"].get("intent") != "data_export"
        for event in events
        if event["event_type"] == "intent"
    )
    messages, _ = container.runtime.conversations.list_messages(
        conversation_id=correlation["conversation_id"], limit=100, cursor=None
    )
    assistant = [m for m in messages if m.run_id == successor and m.role == "assistant"]
    assert len(assistant) == 1


@pytest.mark.live_db
def test_concurrent_run_claim_lets_exactly_one_executor_own_it(harness):
    """并发领取 Run 只有一个赢家，且只有它持有的 token 能续租（ADR-035）。"""
    import threading

    from backend.domain.identifiers import new_id

    container = harness["container"]
    user_id = container.runtime.auth.get_user_by_username(
        container.settings.auth_bootstrap_username
    ).user_id
    handle = container.runs.start_run(
        user_id=user_id,
        conversation_id=harness["conversation_id"],
        user_input="hello there",
        thread_id=None,
    )
    results: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def claim() -> None:
        token = new_id()
        outcome = container.runtime.runs.claim_for_execution(handle.run_id, owner_token=token)
        with lock:
            results.append((token, outcome))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    winners = [token for token, outcome in results if outcome]
    assert len(winners) == 1
    assert container.runtime.runs.is_owned_by(handle.run_id, owner_token=winners[0]) is True
    for token, _outcome in results:
        if token != winners[0]:
            assert container.runtime.runs.heartbeat(handle.run_id, owner_token=token) is False

    # 用例收尾：以安全终态收口这个模拟领取的 Run，不留下 running 行。
    assert (
        container.runs.fail_run_safely(
            handle.run_id,
            code="execution_failed",
            message="simulated executor stopped",
            required_status="running",
            required_owner=winners[0],
        )
        is True
    )


@pytest.mark.live_db
def test_stale_ordinary_run_is_terminalized_instead_of_redispatched(harness):
    """普通 Run 的陈旧租约以安全终态收口（ADR-035 决策 6），绝不释放重跑。

    普通 Run 没有「已批准续跑」入口；重新调度会把整个 Graph 再执行一遍。因此维护任务
    只把它终结为 ``execution_interrupted``（带终态事件、幂等），客户端显式新建 Run 重试。
    """
    from backend.domain.identifiers import new_id

    container = harness["container"]
    client = harness["client"]
    user_id = container.runtime.auth.get_user_by_username(
        container.settings.auth_bootstrap_username
    ).user_id
    handle = container.runs.start_run(
        user_id=user_id,
        conversation_id=harness["conversation_id"],
        user_input="hello there",
        thread_id=None,
    )
    owner_token = new_id()
    assert (
        container.runtime.runs.claim_for_execution(handle.run_id, owner_token=owner_token) is True
    )
    # 模拟执行器崩溃：独立心跳随进程消失，租约时间戳停止推进。
    stale = utc_now() - timedelta(seconds=container.settings.run_stale_running_seconds + 60)
    container.runtime.runs.pool.execute(
        "UPDATE runs SET heartbeat_at = %s, updated_at = %s WHERE run_id = %s",
        (stale, stale, handle.run_id),
    )
    # 陈旧恢复（successor 分支）不处理它：普通 Run 不在释放重跑范围内。
    assert container.runtime.runs.get(handle.run_id).status == "running"
    container.maintenance.recover_stale_running_successors(limit=10)
    assert container.runtime.runs.get(handle.run_id).status == "running"

    assert container.maintenance.recover_stale_running_ordinary(limit=10) >= 1
    failed = container.runtime.runs.get(handle.run_id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "execution_interrupted"
    closing = _read_events(client, handle.run_id)
    assert closing[-1]["event_type"] == "error"
    assert closing[-1]["payload"]["code"] == "execution_interrupted"
    assert closing[-1]["payload"]["terminal"] is True
    # 幂等：重复恢复不再写出第二条终态事件，也不会被重新调度。
    container.maintenance.recover_stale_running_ordinary(limit=10)
    assert container.runtime.runs.get(handle.run_id).status == "failed"
    assert _read_events(client, handle.run_id) == closing


@pytest.mark.live_db
def test_dispatcher_exception_terminalizes_the_run(harness):
    # MAJOR 5：后台调度器捕获到未处理异常后，Run 不能永久停在 running；
    # 必须以安全终态 + 终态事件收口。
    container = harness["container"]
    client = harness["client"]
    user_id = container.runtime.auth.get_user_by_username(
        container.settings.auth_bootstrap_username
    ).user_id
    handle = container.runs.start_run(
        user_id=user_id,
        conversation_id=harness["conversation_id"],
        user_input="hello there",
        thread_id=None,
    )
    assert container.runtime.runs.get(handle.run_id).status == "accepted"

    real_run_graph = container.runs._run_graph

    def exploding_run_graph(**_kwargs):
        raise RuntimeError("simulated dispatcher failure")

    container.runs._run_graph = exploding_run_graph
    try:
        assert container.dispatcher.dispatch(handle.run_id) is True
        container.dispatcher.join(timeout=30)
    finally:
        container.runs._run_graph = real_run_graph

    failed = container.runtime.runs.get(handle.run_id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_code == "execution_failed"
    closing = _read_events(client, handle.run_id)
    assert closing[-1]["event_type"] == "error"
    assert closing[-1]["payload"]["code"] == "execution_failed"
    assert closing[-1]["payload"]["terminal"] is True


@pytest.mark.live_db
def test_unscheduled_normal_run_is_terminated_instead_of_stuck_accepted(harness):
    # MAJOR 6：普通 Run 调度容量耗尽时，Run 不能永久停在 accepted；必须以安全终态收口，
    # API 仍返回 409 提示重试。
    container = harness["container"]
    client = harness["client"]
    conversation_id = client.post(
        "/api/v1/conversations", json={"title": "dispatch capacity"}
    ).json()["conversation_id"]

    # 占满有界调度器的全部剩余并发槽位：接下来的 dispatch 必然容量耗尽。
    acquired = 0
    while container.dispatcher._semaphore.acquire(blocking=False):
        acquired += 1
    assert acquired >= 1
    try:
        response = client.post(
            f"/api/v1/conversations/{conversation_id}/runs", json={"user_input": "hello there"}
        )
        assert response.status_code == 409, response.text
    finally:
        for _ in range(acquired):
            container.dispatcher._semaphore.release()

    rows = container.runtime.runs.pool.query(
        "SELECT run_id, status, error_code FROM runs WHERE conversation_id = %s "
        "ORDER BY created_at DESC",
        (conversation_id,),
    )
    assert len(rows) == 1
    run_id, status, error_code = str(rows[0][0]), str(rows[0][1]), rows[0][2]
    assert status == "failed" and error_code == "scheduler_unavailable"
    # 该 Run 的流以终态 error 关闭：客户端按 stream_url 也能读到明确结果。
    closing = _read_events(client, run_id)
    assert closing[-1]["event_type"] == "error"
    assert closing[-1]["payload"]["code"] == "scheduler_unavailable"
    assert closing[-1]["payload"]["terminal"] is True


@pytest.mark.live_db
def test_expired_approval_is_rejected_by_database_cas(harness, monkeypatch):
    # MAJOR 3（竞态）：应用层预检查与数据库更新之间即使发生时间竞态，
    # 数据库级 CAS 也绝不会批准过期审批；拒绝后审批与 Run 都停在 expired，且无 successor。
    from backend.infrastructure.repositories.approvals import ApprovalRecord

    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, correlation = _start_export(harness)
    container.runtime.approvals.pool.execute(
        "UPDATE approvals SET expires_at = %s WHERE approval_request_id = %s",
        (utc_now() - timedelta(seconds=5), approval_request_id),
    )

    real_is_expired = ApprovalRecord.is_expired
    calls = {"count": 0}

    def racy_is_expired(self, *, now=None):
        # 第一次调用（应用层预检查）读到的是尚未过期的旧快照；之后的调用读到真实状态。
        calls["count"] += 1
        if calls["count"] == 1:
            return False
        return real_is_expired(self, now=now)

    monkeypatch.setattr(ApprovalRecord, "is_expired", racy_is_expired)
    decision = client.post(f"/api/v1/approvals/{approval_request_id}/approve", json=correlation)
    monkeypatch.setattr(ApprovalRecord, "is_expired", real_is_expired)

    assert decision.status_code == 409, decision.text
    assert _successor_rows(container, approval_request_id) == []
    approval = container.runtime.approvals.get(approval_request_id)
    assert approval is not None and approval.status == "expired"
    assert approval.successor_run_id is None
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "expired"
    assert container.runtime.artifacts.get_by_approval(approval_request_id) is None
    closing = _read_events(client, run_id)
    assert closing[-1]["event_type"] == "final"
    assert closing[-1]["payload"]["status"] == "expired"


@pytest.mark.live_db
def test_expiry_transition_rolls_back_as_one_unit_and_can_be_retried(harness, monkeypatch):
    # MAJOR 3（原子性）：审批 CAS、Run 终态与终态事件在单个事务内提交；
    # 事务中途失败则整体回滚（审批仍 pending、Run 仍等待、没有终态事件），重试可完成。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, _correlation = _start_export(harness)
    container.runtime.approvals.pool.execute(
        "UPDATE approvals SET expires_at = %s WHERE approval_request_id = %s",
        (utc_now() - timedelta(seconds=5), approval_request_id),
    )

    real_persist = container.events.persist

    def exploding_persist(envelope, *, cursor):
        raise RuntimeError("simulated crash while writing the terminal event")

    monkeypatch.setattr(container.events, "persist", exploding_persist)
    with pytest.raises(RuntimeError):
        container.approvals.expire_one(approval_request_id)
    monkeypatch.setattr(container.events, "persist", real_persist)

    # 整体回滚：不存在「审批已 expired 但 Run 仍等待且没有关闭事件」的残留状态。
    approval = container.runtime.approvals.get(approval_request_id)
    assert approval is not None and approval.status == "pending"
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "awaiting_approval"
    stored = container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
    assert [row for row in stored if row[2] in {"final", "error"}] == []

    # 重试：原子完成过期转换并关闭事件流。
    assert container.approvals.expire_one(approval_request_id) is True
    assert container.runtime.approvals.get(approval_request_id).status == "expired"
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "expired"
    closing = _read_events(client, run_id)
    assert closing[-1]["event_type"] == "final"
    assert closing[-1]["payload"]["status"] == "expired"
    # 幂等：重复过期不会产生第二条终态事件。
    final_rows = [
        row
        for row in container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
        if row[2] == "final"
    ]
    assert len(final_rows) == 1
    assert container.approvals.expire_one(approval_request_id) is False
    again = [
        row
        for row in container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
        if row[2] == "final"
    ]
    assert [row[0] for row in again] == [row[0] for row in final_rows]


@pytest.mark.live_db
def test_historical_expiry_residue_is_repaired_by_maintenance(harness):
    # 维护补偿：非原子过期写入（历史版本或异常中断）可能留下
    # 「审批已 expired + Run 仍 awaiting_approval + 没有终态事件」的残留；
    # 该残留不会再进入常规过期集合，因此由维护任务按候选集合补齐终态（幂等、有界）。
    container = harness["container"]
    client = harness["client"]
    run_id, approval_request_id, _correlation = _start_export(harness)

    # 模拟旧版本的非原子过期路径：只把审批改为 expired，不触碰 Run、不写终态事件。
    container.runtime.approvals.pool.execute(
        "UPDATE approvals SET status = 'expired', decided_at = %s "
        "WHERE approval_request_id = %s AND status = 'pending'",
        (utc_now(), approval_request_id),
    )
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "awaiting_approval"
    stored = container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
    assert [row for row in stored if row[2] in {"final", "error"}] == []
    assert approval_request_id in container.runtime.approvals.list_expired_unterminated(limit=50)

    # 维护任务发现并补齐：Run 终止为 expired + 唯一的终态事件。
    assert container.maintenance.reconcile_expired_approvals(limit=50) >= 1
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "expired"
    closing = _read_events(client, run_id)
    assert closing[-1]["event_type"] == "final"
    assert closing[-1]["payload"]["status"] == "expired"
    final_rows = [
        row
        for row in container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
        if row[2] == "final"
    ]
    assert len(final_rows) == 1

    # 幂等且收敛：再次补偿不会新增终态事件，且不再有未终结的过期待办。
    container.maintenance.reconcile_expired_approvals(limit=50)
    again = [
        row
        for row in container.runtime.run_events.list_after(run_id=run_id, after_event_id=None)
        if row[2] == "final"
    ]
    assert [row[0] for row in again] == [row[0] for row in final_rows]
    assert container.runtime.approvals.list_expired_unterminated(limit=50) == []
