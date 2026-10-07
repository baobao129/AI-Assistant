"""API 依赖项：运行时 Container、dispatcher 与 bearer 认证。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from fastapi import Header, Request

from backend.application.approvals import ApprovalService
from backend.application.auth import AuthService
from backend.application.events import RunEventPublisher
from backend.application.runs import RunService
from backend.application.runtime import Runtime, build_runtime
from backend.domain.auth import AuthenticatedUser
from backend.domain.checkpoint import open_checkpointer
from backend.domain.errors import authentication_error
from backend.infrastructure.config import Settings
from backend.tools.registry import ToolExecutor
from backend.workers.ingestion import IngestionWorker, MaintenanceWorker

logger = logging.getLogger(__name__)


@dataclass
class Container:
    """进程级装配：运行时、各服务、checkpointer 与 workers。"""

    settings: Settings
    runtime: Runtime
    auth: AuthService
    approvals: ApprovalService
    runs: RunService
    events: RunEventPublisher
    tools: ToolExecutor
    ingestion_worker: IngestionWorker
    maintenance: MaintenanceWorker
    _checkpointer_context: object | None = field(default=None)

    def close(self) -> None:
        self.maintenance.stop()
        if self._checkpointer_context is not None:
            self._checkpointer_context.__exit__(None, None, None)
        self.runtime.close()


def build_tool_registry(runtime: Runtime):
    from backend.tools.data_tools import (
        DatabaseQueryTool,
        DatabaseSchemaTool,
        DataExportTool,
        KnowledgeSearchTool,
    )
    from backend.tools.registry import ToolRegistry

    # 所有 Agent 工具只能经 Tool Registry / Tool Executor 调用，此处集中装配工具集。
    return ToolRegistry(
        [
            KnowledgeSearchTool(
                settings=runtime.settings,
                embeddings=runtime.embeddings,
                vectors=runtime.vectors,
                versions=runtime.versions,
                documents=runtime.documents,
                chunks=runtime.chunks,
            ),
            DatabaseSchemaTool(settings=runtime.settings, query_runner=runtime.query_runner),
            DatabaseQueryTool(
                settings=runtime.settings,
                validator=runtime.validator,
                query_runner=runtime.query_runner,
                results=runtime.query_results,
            ),
            DataExportTool(
                settings=runtime.settings,
                approvals=runtime.approvals,
                results=runtime.query_results,
                artifacts=runtime.artifacts,
            ),
        ]
    )


def build_container(settings: Settings) -> Container:
    # 启动时先恢复陈旧的 ingestion Job，再把各适配器与服务组装进 Container。
    runtime = build_runtime(settings)
    runtime.ingestion.recover_stale_jobs()
    tools = ToolExecutor(build_tool_registry(runtime))
    # Checkpoint 连接需在进程存活期间保持打开，由 Container.close() 统一释放。
    checkpointer_context = open_checkpointer(
        settings.checkpoint_mode, settings.checkpoint_sqlite_path
    )
    checkpointer = checkpointer_context.__enter__()
    events = RunEventPublisher(
        repository=runtime.run_events, retention_events=settings.sse_replay_retention_events
    )
    auth = AuthService(settings=settings, repository=runtime.auth)
    auth.bootstrap_demo_user()
    runs = RunService(
        settings=settings,
        conversations=runtime.conversations,
        threads=runtime.threads,
        runs=runtime.runs,
        documents=runtime.documents,
        approvals=runtime.approvals,
        events=events,
        checkpointer=checkpointer,
        tool_executor=tools,
    )
    approvals = ApprovalService(
        approvals=runtime.approvals,
        runs=runtime.runs,
        threads=runtime.threads,
        events=events,
    )
    ingestion_worker = IngestionWorker(runtime=runtime)
    maintenance = MaintenanceWorker(runtime=runtime)
    return Container(
        settings=settings,
        runtime=runtime,
        auth=auth,
        approvals=approvals,
        runs=runs,
        events=events,
        tools=tools,
        ingestion_worker=ingestion_worker,
        maintenance=maintenance,
        _checkpointer_context=checkpointer_context,
    )


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - app factory always sets it
        raise authentication_error("The application is not ready.")
    return container


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    # 认证边界：仅接受 Bearer token，缺失或格式不符一律按未认证处理。
    if not authorization or not authorization.lower().startswith("bearer "):
        raise authentication_error()
    token = authorization.split(" ", 1)[1].strip()
    return get_container(request).auth.resolve_token(token)


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise authentication_error()
    return authorization.split(" ", 1)[1].strip()


@dataclass
class RunDispatcher:
    """Agent Run 与审批后继执行的有界后台执行器。"""

    container: Container
    max_concurrent: int = 4
    _semaphore: threading.BoundedSemaphore = field(init=False)

    def __post_init__(self) -> None:
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)

    def dispatch(self, run_id: str, *, approval_request_id: str | None = None) -> bool:
        # 信号量限制并发执行数；容量耗尽时立即失败，由上层返回 conflict，绝不无限排队。
        if not self._semaphore.acquire(blocking=False):
            logger.warning("run_dispatcher_capacity_exhausted")
            return False
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, approval_request_id),
            name=f"run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _execute(self, run_id: str, approval_request_id: str | None) -> None:
        try:
            if approval_request_id:
                self.container.runs.resume(run_id, approval_request_id=approval_request_id)
            else:
                self.container.runs.execute(run_id)
        except Exception as error:  # noqa: BLE001 - background execution must not crash
            logger.warning("run_dispatch_failed: %s", type(error).__name__)
        finally:
            self._semaphore.release()

    def join(self, timeout: float | None = None) -> None:
        for thread in list(threading.enumerate()):
            if thread.name.startswith("run-"):
                thread.join(timeout=timeout)


def get_dispatcher(request: Request) -> RunDispatcher:
    dispatcher = getattr(request.app.state, "dispatcher", None)
    if dispatcher is None:  # pragma: no cover
        dispatcher = RunDispatcher(container=get_container(request))
        request.app.state.dispatcher = dispatcher
    return dispatcher
