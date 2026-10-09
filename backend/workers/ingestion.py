"""后台 worker：ingestion 执行、启动恢复与保留期清理。

Ingestion 使用 FastAPI BackgroundTasks，并配合持久化的 Job 生命周期（ADR-009）。
启动恢复会把陈旧的 ``running`` Job 和孤立的 ``pending`` Job 标记为
``failed``；重试始终保持显式且有界。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from backend.application.approvals import ApprovalService
from backend.application.events import RunEventPublisher
from backend.application.ingestion import IngestionService
from backend.application.runtime import Runtime
from backend.application.terminal import TerminalTransition, record_terminal_transition
from backend.domain.identifiers import utc_now

logger = logging.getLogger(__name__)


def _seconds_ago(seconds: int) -> datetime:
    return utc_now() - timedelta(seconds=seconds)


class SuccessorDispatcher(Protocol):
    """维护任务可用的有界调度入口（由组合根注入的 RunDispatcher）。"""

    def dispatch(self, run_id: str, *, approval_request_id: str | None = None) -> bool: ...


@dataclass
class IngestionWorker:
    """在进程自有的有界线程池上运行 ingestion job。"""

    runtime: Runtime
    _threads: list[threading.Thread] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def submit(self, *, ingestion_job_id: str) -> bool:
        if not self.runtime.try_claim_worker_slot():
            logger.warning("ingestion_worker_capacity_exhausted")
            return False
        thread = threading.Thread(
            target=self._run,
            args=(ingestion_job_id,),
            name=f"ingestion-{ingestion_job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def _run(self, ingestion_job_id: str) -> None:
        try:
            # 原文件已持久化在受控存储中，worker 不再接收内存中的文件副本：
            # 进程重启后同一 Job 仍可继续处理（见 CR-RUNTIME-20261009-01）。
            self.runtime.ingestion.process_job(ingestion_job_id)
        except Exception as error:  # noqa: BLE001 - worker thread must not die silently
            logger.warning("ingestion_worker_failed: %s", type(error).__name__)
        finally:
            self.runtime.release_worker_slot()

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)


@dataclass
class MaintenanceWorker:
    """周期性有界维护：审批过期、Query Result 保留期清理与 successor 调度恢复。"""

    runtime: Runtime
    # 事件发布器：崩溃恢复把 successor 终止为安全失败时需要写入终态事件。
    events: RunEventPublisher
    # 审批过期必须经过 Application 层：过期是终态转换（审批生命周期 + 原 Run 终态 +
    # 关闭事件同一事务），不能只改审批行而让 Run 永久停在 awaiting_approval。
    approvals: ApprovalService
    # 摄取 worker：维护任务用它补调度「已持久化但容量耗尽时未能立即调度」的 pending Job。
    ingestion_worker: IngestionWorker
    interval_seconds: float = 60.0
    # 已批准但从未开始执行的后继 Run 的重新调度入口（有界 dispatcher）。
    # 由组合根在 Container 装配完成后注入；缺失时跳过恢复步骤。
    dispatcher: SuccessorDispatcher | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001
                logger.warning("maintenance_failed: %s", type(error).__name__)

    def run_once(self) -> dict[str, int]:
        # 审批过期走 Application 服务：过期的审批与其 Run 终态、关闭事件在同一事务提交。
        expired = self.approvals.expire_due(limit=50)
        results = self.runtime.query_results.delete_expired(limit=200)
        stale = self.recover_stale_running_successors(limit=20)
        return {
            "expired_approvals": len(expired),
            "repaired_expired_runs": self.reconcile_expired_approvals(limit=20),
            "deleted_query_results": results,
            "released_successors": stale["released"],
            "failed_successors": stale["failed"],
            # 普通 Run 的陈旧租约以安全终态收口（不重新调度，见 ADR-035）。
            "failed_ordinary_runs": self.recover_stale_running_ordinary(limit=20),
            # 先释放陈旧 claiming，再重新调度：同一周期内即可把可恢复的 successor 送回执行。
            "recovered_successors": self.recover_pending_successors(limit=20),
            # 上传时调度容量耗尽的摄取 Job 同样以持久化 pending 保留，由本步骤补调度。
            "scheduled_pending_jobs": self.schedule_pending_jobs(),
        }

    def schedule_pending_jobs(self, *, limit: int | None = None) -> int:
        """把「已持久化但未被任何 worker 领取」的 pending 摄取 Job 补调度（有界）。

        上传路径在容量耗尽时不再拒绝请求：原文件与 Job 已经持久化，Job 保持 ``pending``，
        由本步骤在后续周期用既有的有界 worker 重新调度。``claim`` 是原子的，因此补调度
        不会让同一 Job 被执行两次；容量仍不足时本轮跳过，下一周期继续。
        """

        batch = limit if limit is not None else self.runtime.settings.ingestion_schedule_batch
        older_than = _seconds_ago(self.runtime.settings.ingestion_pending_schedule_seconds)
        scheduled = 0
        for ingestion_job_id in self.runtime.jobs.list_pending(older_than=older_than, limit=batch):
            if not self.ingestion_worker.submit(ingestion_job_id=ingestion_job_id):
                logger.warning("pending_job_schedule_capacity_exhausted")
                break
            scheduled += 1
        return scheduled

    def reconcile_expired_approvals(self, *, limit: int = 20) -> int:
        """补齐「审批已 expired 但 Run 仍未终结」的残留（幂等、有界）。

        非原子的过期写入（历史版本或异常中断）可能留下这类行；它们不再是 ``pending``，
        因此常规过期流程不会再次处理。本步骤对每条残留补做 Run 的条件终结与终态事件，
        使缺口可自愈，无需人工改库。
        """

        repaired = 0
        for approval_request_id in self.runtime.approvals.list_expired_unterminated(limit=limit):
            if self.approvals.finalize_expired(approval_request_id):
                repaired += 1
        return repaired

    def recover_stale_running_successors(self, *, limit: int = 20) -> dict[str, int]:
        """有界恢复受进程崩溃影响的 successor（六-A/B 的 ``running`` 分支）。

        - 只处理带审批绑定（``approval_request_id`` + ``original_run_id``）的 successor，
          且租约时间戳（``COALESCE(heartbeat_at, updated_at)``）超过
          ``run_stale_running_seconds``（独立心跳线程会持续刷新它，因此仍在推进的长任务
          不会被误改）；
        - 尝试次数以该 Run 已持久化的 ``agent_start`` 事件数为权威来源：``execute()``
          与 ``resume()`` 都在领取成功后发布该事件，所以真实崩溃恢复同样会消耗预算；
        - 未超过尝试上限：条件更新 ``running -> accepted``，由同一周期的
          ``recover_pending_successors()`` 重新调度，重新经过 ``claim_for_execution()``，
          因此最多一个执行者（GRAPH-007）；导出副作用由审批级唯一约束保持「最多一次」；
        - 达到 ``run_max_recovery_attempts``：终止为明确安全失败并写入终态事件，
          避免崩溃恢复进入无限重试。
        """

        older_than = _seconds_ago(self.runtime.settings.run_stale_running_seconds)
        stale = self.runtime.runs.list_stale_running_successors(older_than=older_than, limit=limit)
        released = 0
        failed = 0
        for run_id in stale:
            attempts = self.runtime.run_events.count_of_type(
                run_id=run_id, event_type="agent_start"
            )
            if attempts >= self.runtime.settings.run_max_recovery_attempts:
                run = self.runtime.runs.get(run_id)
                if run is None:
                    continue
                if record_terminal_transition(
                    pool=self.runtime.application_pool,
                    runs=self.runtime.runs,
                    events=self.events,
                    transition=TerminalTransition(
                        run_id=run_id,
                        thread_id=run.thread_id,
                        status="failed",
                        event_type="error",
                        payload={
                            "code": "execution_interrupted",
                            "message": (
                                "The approved execution was interrupted and could not be "
                                "recovered within its retry budget."
                            ),
                            "terminal": True,
                        },
                        logical_transition="recovery_exhausted",
                        required_status="running",
                        error_code="execution_interrupted",
                        error_message="Recovery attempts exhausted after an interrupted execution.",
                    ),
                ):
                    failed += 1
                continue
            if self.runtime.runs.release_stale_claim(run_id, older_than=older_than):
                released += 1
        return {"released": released, "failed": failed}

    def recover_stale_running_ordinary(self, *, limit: int = 20) -> int:
        """把租约已陈旧的普通（非 HITL）``running`` Run 安全终止为失败（有界、幂等）。

        普通 Run 没有「已批准续跑」入口，重新调度会把整个 Graph 再执行一遍
        （新的 Query Result Snapshot、新的生成），因此这里选择**安全终态**：
        以 ``execution_interrupted`` 终结并入终态事件，客户端可显式新建 Run 重试
        （ADR-035 决策 6）。陈旧判定基于独立心跳续租的租约时间戳，仍在执行的长任务
        不会被误终结。
        """

        older_than = _seconds_ago(self.runtime.settings.run_stale_running_seconds)
        stale = self.runtime.runs.list_stale_running_ordinary(older_than=older_than, limit=limit)
        failed = 0
        for run_id in stale:
            run = self.runtime.runs.get(run_id)
            if run is None:
                continue
            if record_terminal_transition(
                pool=self.runtime.application_pool,
                runs=self.runtime.runs,
                events=self.events,
                transition=TerminalTransition(
                    run_id=run_id,
                    thread_id=run.thread_id,
                    status="failed",
                    event_type="error",
                    payload={
                        "code": "execution_interrupted",
                        "message": (
                            "The execution stopped unexpectedly. Please submit the request "
                            "again as a new run."
                        ),
                        "terminal": True,
                    },
                    logical_transition="ordinary_execution_interrupted",
                    required_status="running",
                    error_code="execution_interrupted",
                    error_message="The execution lease expired without a completing executor.",
                ),
            ):
                failed += 1
        return failed

    def recover_pending_successors(self, *, limit: int = 20) -> int:
        """有界恢复：重新调度「已批准但从未被领取执行」的后继 Run。

        这些行就是持久化的调度意图——进程在审批提交后、调度开始前退出，或调度容量
        一度耗尽时，后继 Run 会停留在 ``accepted``；本步骤按周期重试 dispatch，
        由 Run 的原子领取保证最多执行一次。容量仍不足时本轮跳过，下一周期继续。
        """

        if self.dispatcher is None:
            return 0
        recovered = 0
        for run_id, approval_request_id in self.runtime.approvals.list_pending_successor_run_ids(
            limit=limit
        ):
            if self.dispatcher.dispatch(run_id, approval_request_id=approval_request_id):
                recovered += 1
            else:
                logger.warning("successor_recovery_capacity_exhausted")
                break
        return recovered


def recover_startup_state(runtime: Runtime) -> dict[str, int]:
    """对陈旧的 ingestion Job 执行显式的有界恢复。"""

    return runtime.ingestion.recover_stale_jobs()


__all__ = ["IngestionService", "IngestionWorker", "MaintenanceWorker", "recover_startup_state"]
