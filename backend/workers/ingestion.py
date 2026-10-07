"""后台 worker：ingestion 执行、启动恢复与保留期清理。

Ingestion 使用 FastAPI BackgroundTasks，并配合持久化的 Job 生命周期（ADR-009）。
启动恢复会把陈旧的 ``running`` Job 和孤立的 ``pending`` Job 标记为
``failed``；重试始终保持显式且有界。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from backend.application.ingestion import IngestionService
from backend.application.runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass
class IngestionWorker:
    """在进程自有的有界线程池上运行 ingestion job。"""

    runtime: Runtime
    _threads: list[threading.Thread] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def submit(self, *, ingestion_job_id: str, payload: bytes | None) -> bool:
        if not self.runtime.try_claim_worker_slot():
            logger.warning("ingestion_worker_capacity_exhausted")
            return False
        thread = threading.Thread(
            target=self._run,
            args=(ingestion_job_id, payload),
            name=f"ingestion-{ingestion_job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads.append(thread)
        thread.start()
        return True

    def _run(self, ingestion_job_id: str, payload: bytes | None) -> None:
        try:
            self.runtime.ingestion.process_job(ingestion_job_id, payload=payload)
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
    """周期性有界维护：审批过期与 Query Result 保留期清理。"""

    runtime: Runtime
    interval_seconds: float = 60.0
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
        expired = self.runtime.approvals.expire_due(limit=50)
        results = self.runtime.query_results.delete_expired(limit=200)
        return {"expired_approvals": len(expired), "deleted_query_results": results}


def recover_startup_state(runtime: Runtime) -> dict[str, int]:
    """对陈旧的 ingestion Job 执行显式的有界恢复。"""

    return runtime.ingestion.recover_stale_jobs()


__all__ = ["IngestionService", "IngestionWorker", "MaintenanceWorker", "recover_startup_state"]
