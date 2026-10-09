"""独立租约心跳（CR-RUNTIME-20261009-01 batch 2 / ADR-035）。

执行者按固定周期在后台续租自己的执行声明。心跳**不依赖** Graph 快照、模型输出或
任何步骤边界，因此单步长时间阻塞（大文件解析、大批量 embedding、长 SQL、长导出）
始终在租约覆盖范围内，不会被维护任务误判为「执行者已崩溃」。

语义：

- ``renew()`` 返回 ``False``：数据库侧已撤销租约（终态、已释放、或已归属其他执行者）。
  续租立即停止并置 ``lost``；执行者据此在提交点放弃写入（提交点自身也有所有权条件）。
- ``renew()`` 抛异常：视为基础设施瞬时故障，记录后下一个周期重试。线程生命周期由执行
  本身界定（``stop()``），因此它不是无限重试循环，也不会在执行业务结束时残留线程。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LeaseHeartbeat:
    """有界的后台续租线程；线程本身不写业务数据，只刷新租约时间戳。"""

    interval_seconds: float
    renew: Callable[[], bool]
    name: str = "lease-heartbeat"
    _stop: threading.Event = field(default_factory=threading.Event)
    _lost: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        """租约是否已被撤销（由心跳线程发现，或由执行者在提交点发现）。"""

        return self._lost.is_set()

    def mark_lost(self) -> None:
        """执行者在提交点发现失去所有权时同步置位，使后续检查短路。"""

        self._lost.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        # 首次续租等一个周期：领取动作本身已经写入初始 heartbeat_at，无需立即重复写。
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # 有界等待：即使某次续租调用卡住，也不会阻塞业务收尾（线程是 daemon）。
            thread.join(timeout=max(1.0, float(self.interval_seconds) * 2))

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not self.renew():
                    self._lost.set()
                    logger.info("lease_revoked: %s", self.name)
                    return
            except Exception as error:  # noqa: BLE001 - 心跳失败不得中断业务执行
                logger.warning("lease_renew_failed: %s: %s", self.name, type(error).__name__)


__all__ = ["LeaseHeartbeat"]
