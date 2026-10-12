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
from backend.domain.identifiers import new_id
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
    # 有界 Run 调度器：API 与维护任务（successor 恢复）共享同一个实例。
    dispatcher: RunDispatcher | None = None
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
                registry=runtime.registry,
                reranker=runtime.reranker,
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
        # 终态转换的事务边界：Run 终态与其终态事件必须同一事务提交。
        pool=runtime.application_pool,
        # 父块提供者（ADR-038）：RAG 子图据此展开真实父块，而不是只做直通过滤。
        parent_context=runtime.parent_context,
    )
    approvals = ApprovalService(
        approvals=runtime.approvals,
        runs=runtime.runs,
        threads=runtime.threads,
        events=events,
        # 决策事务边界：审批 CAS、successor 创建与原始 Run 终态在同一事务内提交。
        pool=runtime.application_pool,
    )
    ingestion_worker = IngestionWorker(runtime=runtime)
    maintenance = MaintenanceWorker(
        runtime=runtime, events=events, approvals=approvals, ingestion_worker=ingestion_worker
    )
    container = Container(
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
    # 有界调度器在 Container 装配完成后创建：API 与维护任务的 successor 恢复共用它。
    container.dispatcher = RunDispatcher(container=container)
    maintenance.dispatcher = container.dispatcher
    return container


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

    def dispatch(
        self,
        run_id: str,
        *,
        approval_request_id: str | None = None,
        document_scope: tuple[str, ...] = (),
    ) -> bool:
        # 信号量限制并发执行数；容量耗尽时立即失败，由上层返回 conflict，绝不无限排队。
        if not self._semaphore.acquire(blocking=False):
            logger.warning("run_dispatcher_capacity_exhausted")
            return False
        # 执行所有权 token 由调度器生成：执行者用它领取 Run 并续租，异常兜底也用它
        # 做条件收口，因此失去所有权的旧执行者不会误终结已被重新调度的 Run（ADR-035）。
        owner_token = new_id()
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, approval_request_id, owner_token, document_scope),
            name=f"run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _execute(
        self,
        run_id: str,
        approval_request_id: str | None,
        owner_token: str,
        document_scope: tuple[str, ...] = (),
    ) -> None:
        try:
            if approval_request_id:
                # 审批 successor 走 resume：作用域从**同一个 Thread 的 Checkpoint**
                # 恢复，因此这里刻意不传，避免用空值覆盖已恢复的作用域（ADR-039）。
                self.container.runs.resume(
                    run_id, approval_request_id=approval_request_id, owner_token=owner_token
                )
            else:
                self.container.runs.execute(
                    run_id, owner_token=owner_token, document_scope=document_scope
                )
        except Exception as error:  # noqa: BLE001 - background execution must not crash
            logger.warning("run_dispatch_failed: %s", type(error).__name__)
            # 异常兜底：绝不允许后台执行异常留下永久 ``running``。以安全终态收口并写入
            # 终态事件（CAS + 所有权条件，幂等：已终结或已被新执行者领取的 Run 不会被覆盖）。
            try:
                terminalized = self.container.runs.fail_run_safely(
                    run_id,
                    code="execution_failed",
                    message="The execution could not complete safely.",
                    required_status="running",
                    required_owner=owner_token,
                )
                # An atomic claim can roll back before the Run reaches ``running``.
                # Close that accepted intent too, while preserving CAS ownership if
                # another dispatcher claimed it concurrently. ``accepted`` 无执行者，
                # 因此这一步不需要（也不应带）所有权条件。
                if not terminalized:
                    self.container.runs.fail_run_safely(
                        run_id,
                        code="execution_failed",
                        message="The execution could not complete safely.",
                        required_status="accepted",
                    )
            except Exception:  # noqa: BLE001 - 兜底路径自身不得让后台线程崩溃
                logger.warning("run_dispatch_terminalization_failed: %s", run_id)
        finally:
            self._semaphore.release()

    def join(self, timeout: float | None = None) -> None:
        for thread in list(threading.enumerate()):
            if thread.name.startswith("run-"):
                thread.join(timeout=timeout)


def get_dispatcher(request: Request) -> RunDispatcher:
    container = get_container(request)
    # 组合根已在 Container 上装配共享实例；此处仅为手工装配的测试容器提供兜底。
    if container.dispatcher is None:  # pragma: no cover - build_container 总会装配
        container.dispatcher = RunDispatcher(container=container)
    return container.dispatcher
