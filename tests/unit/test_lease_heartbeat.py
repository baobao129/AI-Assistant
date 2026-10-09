"""独立租约心跳的单元检查（ADR-035）。

心跳线程的语义：
- 按周期续租，不依赖任何步骤边界（不依赖 Graph 快照/模型输出）；
- ``renew()`` 返回 ``False`` 表示数据库已撤销租约 → 置 ``lost`` 并停止续租；
- ``renew()`` 抛异常是瞬时基础设施故障 → 记录后下一周期继续（不中断业务执行）；
- ``stop()`` 幂等且不会阻塞业务收尾。
"""

from __future__ import annotations

import time

from backend.application.lease import LeaseHeartbeat


def _wait_until(predicate, *, deadline: float = 2.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_heartbeat_renews_on_interval_and_reports_not_lost():
    calls: list[int] = []

    def renew() -> bool:
        calls.append(len(calls))
        return True

    heartbeat = LeaseHeartbeat(interval_seconds=0.05, renew=renew, name="unit-renew")
    heartbeat.start()
    try:
        assert _wait_until(lambda: len(calls) >= 2)
        assert heartbeat.lost is False
    finally:
        heartbeat.stop()


def test_heartbeat_latches_lost_and_stops_renewing():
    calls: list[int] = []

    def renew() -> bool:
        calls.append(len(calls))
        return False  # 数据库侧已撤销租约

    heartbeat = LeaseHeartbeat(interval_seconds=0.02, renew=renew, name="unit-lost")
    heartbeat.start()
    try:
        assert _wait_until(lambda: heartbeat.lost)
        assert len(calls) == 1  # 失权后不再继续续租
        time.sleep(0.1)
        assert len(calls) == 1
    finally:
        heartbeat.stop()


def test_heartbeat_survives_transient_failures():
    calls: list[int] = []

    def renew() -> bool:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("simulated transient database failure")
        return True

    heartbeat = LeaseHeartbeat(interval_seconds=0.03, renew=renew, name="unit-transient")
    heartbeat.start()
    try:
        assert _wait_until(lambda: len(calls) >= 3)
        # 瞬时故障不判定失权，也不会停止续租。
        assert heartbeat.lost is False
    finally:
        heartbeat.stop()


def test_stop_is_idempotent_and_safe_without_start():
    heartbeat = LeaseHeartbeat(interval_seconds=10, renew=lambda: True, name="unit-idle")
    heartbeat.stop()  # 未启动也能安全停止
    heartbeat.mark_lost()
    assert heartbeat.lost is True
    heartbeat.start()
    heartbeat.stop()
    heartbeat.stop()
    assert heartbeat.lost is True
