"""对话、Run 生命周期、Graph 执行与 SSE 事件映射（API-004 / API-005）。

Graph 负责执行；本服务负责 Graph State 构建、Tool 执行范围、
Conversation Persistence、Run 状态与 Application Events。
它绝不暴露原始 Graph State、Checkpoint 数据或隐藏推理。

执行所有权（CR-RUNTIME-20261009-01 batch 2 / ADR-035）：领取 Run 时记录
``owner_token``，执行期间由独立心跳线程续租；每个提交点（执行期事件、等待态、
终态）都带所有权条件，失去所有权的执行者不写入任何数据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.application.events import EventEnvelope, RunEventPublisher
from backend.application.lease import LeaseHeartbeat
from backend.application.terminal import (
    TerminalTransition,
    apply_terminal_transition,
    record_terminal_transition,
)
from backend.domain.errors import AppError, ExecutionOwnershipLost, conflict, not_found
from backend.domain.identifiers import new_id
from backend.domain.state import initial_state
from backend.graph.main import build_main_graph
from backend.infrastructure.config import Settings
from backend.infrastructure.database import MySQLConnectionPool
from backend.infrastructure.llm import create_llm_provider
from backend.infrastructure.repositories.approvals import ApprovalRepository
from backend.infrastructure.repositories.conversation import (
    ConversationRepository,
    RunRecord,
    RunRepository,
    ThreadRepository,
)
from backend.infrastructure.repositories.documents import DocumentRepository
from backend.tools.contracts import ToolExecutionContext

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "rejected", "failed", "expired", "handoff_complete"}
HANDOFF_COMPLETE = "handoff_complete"


@dataclass(frozen=True)
class RunHandle:
    conversation_id: str
    thread_id: str
    run_id: str
    status: str


class RunService:
    def __init__(
        self,
        *,
        settings: Settings,
        conversations: ConversationRepository,
        threads: ThreadRepository,
        runs: RunRepository,
        documents: DocumentRepository,
        approvals: ApprovalRepository,
        events: RunEventPublisher,
        checkpointer: object | None,
        tool_executor: Any,
        pool: MySQLConnectionPool,
    ) -> None:
        self.settings = settings
        self.conversations = conversations
        self.threads = threads
        self.runs = runs
        self.documents = documents
        self.approvals = approvals
        self.events = events
        self.checkpointer = checkpointer
        self.tool_executor = tool_executor
        # 终态转换的事务边界：状态与终态事件必须同一事务提交（application.terminal）。
        self.pool = pool

    # 请求处理 ------------------------------------------------------
    def start_run(
        self,
        *,
        user_id: str,
        conversation_id: str,
        user_input: str,
        thread_id: str | None,
        client_metadata: dict[str, Any] | None = None,
    ) -> RunHandle:
        conversation = self.conversations.get(conversation_id, user_id=user_id)
        if conversation is None:
            raise not_found("Conversation was not found.")
        text = user_input.strip()
        if not text:
            raise AppError("validation_error", "A request is required.", status_code=422)
        if thread_id:
            if not self.threads.belongs_to_conversation(
                thread_id=thread_id, conversation_id=conversation_id
            ):
                raise not_found("Thread was not found in this conversation.")
            if self.runs.find_active_run_for_thread(thread_id) is not None:
                raise conflict("This thread already has a non-terminal run.")
        else:
            thread_id = self.threads.create(conversation_id=conversation_id)

        self.conversations.add_message(
            conversation_id=conversation_id,
            role="user",
            content=text,
            status="committed",
            metadata={"client_metadata": client_metadata or {}},
        )
        run = self.runs.create(
            conversation_id=conversation_id,
            thread_id=thread_id,
            user_id=user_id,
            user_input=text,
            client_metadata=client_metadata,
        )
        self.conversations.touch(conversation_id)
        return RunHandle(conversation_id, thread_id, run.run_id, run.status)

    def resolve_run(self, *, user_id: str, run_id: str) -> RunRecord:
        run = self.runs.get(run_id)
        if run is None or run.user_id != user_id:
            raise not_found("Run was not found.")
        return run

    # 执行 -------------------------------------------------------------
    def execute(self, run_id: str, *, owner_token: str | None = None) -> str:
        """领取并执行一个 Run。

        ``owner_token`` 由调度器在派发时生成并保留，使调度器的异常兜底也能带所有权
        条件收口（ADR-035）；未给出时由本方法生成（等价语义）。
        """

        run = self.runs.get(run_id)
        if run is None:
            return "failed"
        if run.status in TERMINAL_STATUSES:
            return run.status
        token = owner_token or new_id()
        # 一个 Run 最多被一个执行器领取；重复 dispatch 不会导致第二次 Graph 执行。
        if not self._claim_and_publish_agent_start(run, owner_token=token):
            current = self.runs.get(run_id)
            return current.status if current else "failed"
        state = initial_state(
            conversation_id=run.conversation_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            user_input=run.user_input,
        )
        return self._run_graph(run=run, state=state, approval_request_id=None, owner_token=token)

    def resume(
        self,
        run_id: str,
        *,
        approval_request_id: str | None = None,
        owner_token: str | None = None,
    ) -> str:
        """在同一 Thread 与 Checkpoint 沿革下继续一个被中断的 Run。

        幂等边界：只有仍处于 ``accepted`` 的后继 Run 才能被领取并执行一次；已终态、
        正在执行或已被其他调用领取的 Run 直接返回当前状态，绝不重复执行 Graph。
        """

        run = self.runs.get(run_id)
        if run is None:
            return "failed"
        if run.status in TERMINAL_STATUSES:
            return run.status
        token = owner_token or new_id()
        if not self._claim_and_publish_agent_start(run, owner_token=token):
            current = self.runs.get(run_id)
            return current.status if current else "failed"

        # Resume 从被中断 Run 的 Checkpoint/可恢复 State 继续：本次只传入「后继 Identity +
        # approval 绑定」这一有界增量，messages、tool_results、repair_attempts、
        # answer_revision_count、query_result_id 与已完成的 Tool 执行事实都由 Checkpoint
        # 恢复，绝不用 initial_state() 覆盖（GRAPH_CONTRACT Graph-005）。
        bound_query_result_id: str | None = None
        if approval_request_id:
            approval = self.approvals.get(approval_request_id)
            if approval is not None and approval.status == "approved":
                # Approval 绑定了一个不可变快照；resume 复用完全相同的那一个。
                bound_query_result_id = str(approval.binding.get("query_result_id") or "") or None
        state: dict[str, Any] = {
            "conversation_id": run.conversation_id,
            "thread_id": run.thread_id,
            "run_id": run.run_id,
            "user_input": run.user_input,
            "approval_request_id": approval_request_id,
            "interrupt_status": "approved" if approval_request_id else "none",
            "query_result_id": bound_query_result_id,
        }
        return self._run_graph(
            run=run,
            state=state,
            approval_request_id=approval_request_id,
            bound_query_result_id=bound_query_result_id,
            owner_token=token,
        )

    def _claim_and_publish_agent_start(self, run: RunRecord, *, owner_token: str) -> bool:
        """Atomically claim a Run and persist its execution-attempt marker.

        The event is committed with ``accepted -> running``. A crash before commit
        leaves the Run schedulable; a crash after commit cannot evade recovery-budget
        accounting even if process fan-out never runs.
        """

        prepared = self.events.prepare(
            run_id=run.run_id,
            thread_id=run.thread_id,
            event_type="agent_start",
            payload={"status": "running"},
        )
        with self.pool.transaction() as cursor:
            if not self.runs.claim_for_execution_with_cursor(
                cursor, run.run_id, owner_token=owner_token
            ):
                return False
            persisted = self.events.persist(prepared, cursor=cursor)
        self.events.notify(persisted)
        return True

    def _run_graph(
        self,
        *,
        run: RunRecord,
        state: dict[str, Any],
        approval_request_id: str | None,
        owner_token: str,
        bound_query_result_id: str | None = None,
    ) -> str:
        context = ToolExecutionContext(
            conversation_id=run.conversation_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            user_id=run.user_id,
            approval_request_id=approval_request_id,
            query_result_id=bound_query_result_id,
            authorized_document_ids=self._authorized_document_ids(run.user_id),
        )
        graph = build_main_graph(
            executor=self.tool_executor,
            checkpointer=self.checkpointer,
            settings=self.settings,
            general_llm=create_llm_provider(self.settings),
            context=context,
        )
        config = {"configurable": {"thread_id": run.thread_id}}
        # 独立租约心跳（ADR-035）：不依赖 Graph 快照或模型输出，单步长阻塞也在覆盖范围内。
        heartbeat = LeaseHeartbeat(
            interval_seconds=self.settings.run_heartbeat_seconds,
            renew=lambda: self._heartbeat(run.run_id, owner_token=owner_token),
            name=f"run-heartbeat-{run.run_id[:8]}",
        )
        heartbeat.start()
        try:
            try:
                result = self._stream_graph(
                    graph=graph,
                    state=state,
                    config=config,
                    run=run,
                    owner_token=owner_token,
                    heartbeat=heartbeat,
                )
            except ExecutionOwnershipLost:
                # 已失去所有权：不写终态、不写消息、不写事件，只报告权威状态。
                logger.info("run_execution_ownership_lost run=%s", run.run_id)
                heartbeat.mark_lost()
                current = self.runs.get(run.run_id)
                return current.status if current else "running"
            except Exception as error:  # noqa: BLE001 - Graph failure becomes a safe terminal error
                logger.warning(
                    "graph_execution_failed: %s: %s",
                    type(error).__name__,
                    str(error)[:300],
                )
                return self._fail_run(
                    run,
                    "internal_error",
                    "The request could not complete safely.",
                    owner_token=owner_token,
                )
            return self._finalize_run(run, result, owner_token=owner_token)
        finally:
            heartbeat.stop()

    def _stream_graph(
        self,
        *,
        graph: Any,
        state: dict[str, Any],
        config: dict,
        run: RunRecord,
        owner_token: str,
        heartbeat: LeaseHeartbeat,
    ) -> dict[str, Any]:
        """流式读取 State 快照，使 Application Events 遵循执行顺序。

        执行期事件都带 ``owner_token``（提交点所有权校验）；每个快照边界还显式检查
        所有权，一旦失去立即以 ``ExecutionOwnershipLost`` 中止执行——暴露窗口被限制在
        当时正在执行的单个节点内（ADR-035）。
        """

        latest: dict[str, Any] = state
        seen_intents: set[str] = set()
        seen_retrievals = 0
        seen_calls = 0
        seen_results = 0
        seen_citations = 0
        # Resume 的后继 Run 从 Checkpoint 继承了前序 Run 的 messages/tool_results（追加
        # reducer 语义），它们属于原始 Run 的 SSE 序列。首个快照只作为增量游标的基线，
        # 保证 successor 的独立序列绝不重新发布继承来的 Tool Call / Tool Result（API-005）。
        baseline_ready = False
        # 以增量游标（seen_*）把执行中不断增长的 State 集合映射为一次性事件，
        # 避免同一个 Tool Call、Tool Result 或 Citation 被重复发布。
        for snapshot in graph.stream(state, config, stream_mode="values"):
            if heartbeat.lost or not self.runs.is_owned_by(run.run_id, owner_token=owner_token):
                raise ExecutionOwnershipLost(run.run_id)
            latest = snapshot
            calls = [call for call in (snapshot.get("messages") or []) if call.get("tool_calls")]
            tool_results = snapshot.get("tool_results") or []
            if not baseline_ready:
                seen_calls = len(calls)
                seen_results = len(tool_results)
                baseline_ready = True
            intent = snapshot.get("intent")
            if intent and intent not in seen_intents:
                seen_intents.add(intent)
                self.events.publish(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    event_type="intent",
                    payload={
                        "intent": intent,
                        "routing_outcome": snapshot.get("routing_outcome"),
                        "current_path": snapshot.get("current_path"),
                    },
                    owner_token=owner_token,
                )
            retrieval_results = snapshot.get("retrieval_results") or []
            if len(retrieval_results) > seen_retrievals and retrieval_results:
                seen_retrievals = len(retrieval_results)
                self.events.publish(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    event_type="retrieval",
                    payload={
                        "query": snapshot.get("retrieval_query"),
                        "result_count": len(retrieval_results),
                    },
                    owner_token=owner_token,
                )
            for message in calls[seen_calls:]:
                for call in message.get("tool_calls") or []:
                    self.events.publish(
                        run_id=run.run_id,
                        thread_id=run.thread_id,
                        event_type="tool_call",
                        payload={
                            "tool_id": call.get("tool_id"),
                            "tool_call_id": call.get("tool_call_id"),
                        },
                        owner_token=owner_token,
                    )
            seen_calls = len(calls)
            for result in tool_results[seen_results:]:
                self.events.publish(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    event_type="tool_result",
                    payload={
                        "tool_id": result.get("tool_id"),
                        "status": result.get("status"),
                        "row_count": (result.get("data") or {}).get("row_count"),
                        "truncated": (result.get("data") or {}).get("truncated"),
                        "error_code": (result.get("error") or {}).get("code"),
                    },
                    owner_token=owner_token,
                )
            seen_results = len(tool_results)
            citations = snapshot.get("citations") or []
            for citation in citations[seen_citations:]:
                self.events.publish(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    event_type="citation",
                    payload=citation,
                    owner_token=owner_token,
                )
            seen_citations = len(citations)
        return latest

    def _heartbeat(self, run_id: str, *, owner_token: str) -> bool:
        """续租执行声明；续租失败只削弱陈旧判定，不中断业务执行。

        返回 ``False`` 表示租约已被撤销（Run 已终态、已被释放或已归属他人）：
        独立心跳线程据此停止续租并置 ``lost``，执行者在下一个提交点放弃写入。
        """

        try:
            return self.runs.heartbeat(run_id, owner_token=owner_token)
        except Exception:  # noqa: BLE001 - 心跳是尽力而为的优化
            logger.warning("run_heartbeat_failed: %s", run_id)
            return True  # 基础设施瞬时故障：不判定失权，下一周期重试

    def _finalize_run(self, run: RunRecord, state: dict[str, Any], *, owner_token: str) -> str:
        approval_request_id = state.get("approval_request_id")
        if approval_request_id and state.get("interrupt_status") == "pending":
            # HITL：等待中的 Run 保持 awaiting_approval，业务上并未完成。
            # approval_required payload 按 API-005 提供完整关联字段 + 生命周期 + 过期时间 +
            # 安全的用户可见摘要；摘要与过期时间以 Approval Request 为权威来源。
            approval = self.approvals.get(approval_request_id)
            last_data = (state.get("tool_results") or [{}])[-1].get("data") or {}
            with self.pool.transaction() as cursor:
                # 等待同样是条件更新：只有仍在 running 且仍持有执行所有权的 Run 才能进入
                # awaiting_approval；陈旧执行器不得把已被终结的 Run 拉回等待态。
                applied = self.runs.update_status_with_cursor(
                    cursor,
                    run.run_id,
                    "awaiting_approval",
                    routing_outcome=state.get("routing_outcome"),
                    intent=state.get("intent"),
                    approval_request_id=approval_request_id,
                    # 等待中的 Run 同样记录它已产出的 Query Result Snapshot 引用，
                    # 使审批绑定的快照可以在 Run 上追溯（与终态路径一致）。
                    query_result_id=state.get("query_result_id"),
                    required_status="running",
                    required_owner=owner_token,
                )
            if not applied:
                # 输掉 CAS：Run 已被其他路径终结，不再发布 approval_required。
                current = self.runs.get(run.run_id)
                return current.status if current else "awaiting_approval"
            self.events.publish(
                run_id=run.run_id,
                thread_id=run.thread_id,
                event_type="approval_required",
                payload={
                    "approval_request_id": approval_request_id,
                    "conversation_id": run.conversation_id,
                    "thread_id": run.thread_id,
                    "run_id": run.run_id,
                    "approval_lifecycle": "pending",
                    "expires_at": (
                        approval.expires_at.isoformat()
                        if approval is not None
                        else last_data.get("expires_at")
                    ),
                    "summary": (
                        dict(approval.summary) if approval is not None else last_data.get("summary")
                    ),
                },
            )
            return "awaiting_approval"

        error = state.get("error") or {}
        validation = state.get("answer_validation") or {}
        final_response = state.get("final_response") or ""
        terminal_status = "completed"
        if validation.get("status") == "insufficient_evidence" and not final_response:
            terminal_status = "failed"
        if error and not final_response:
            return self._fail_run(
                run,
                str(error.get("code") or "internal_error"),
                str(error.get("message") or "The request could not complete safely."),
                owner_token=owner_token,
            )
        # 终态转换、assistant 消息与会话时间戳在同一事务内提交，且只有赢得终态 CAS
        # （仍处于 running 且仍持有本次执行所有权）的执行者才写这些副作用：陈旧执行器
        # （例如被恢复流程释放后又重新调度、仍在运行的原执行器）既不覆盖新终态，
        # 也不写出重复回答或第二条终态事件（STATE / API-005 / ADR-035）。
        envelope: EventEnvelope | None = None
        with self.pool.transaction() as cursor:
            envelope = apply_terminal_transition(
                cursor,
                runs=self.runs,
                events=self.events,
                transition=TerminalTransition(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    status=terminal_status,
                    event_type="final",
                    payload={"status": terminal_status, "content": final_response},
                    logical_transition=f"final:{terminal_status}",
                    required_status="running",
                    required_owner=owner_token,
                    routing_outcome=state.get("routing_outcome"),
                    intent=state.get("intent"),
                    final_response=final_response,
                    query_result_id=state.get("query_result_id"),
                ),
            )
            if envelope is not None:
                self.conversations.add_message_with_cursor(
                    cursor,
                    conversation_id=run.conversation_id,
                    role="assistant",
                    content=final_response,
                    status="committed",
                    run_id=run.run_id,
                    citations=list(state.get("citations") or []),
                    metadata={
                        "intent": state.get("intent"),
                        "routing_outcome": state.get("routing_outcome"),
                        "tool_summaries": [
                            {"tool_id": result.get("tool_id"), "status": result.get("status")}
                            for result in (state.get("tool_results") or [])
                        ],
                    },
                )
                self.conversations.touch_with_cursor(cursor, run.conversation_id)
        if envelope is None:
            # 输掉终态 CAS：Run 已由其他执行者（或崩溃恢复、审批决策）终结。
            current = self.runs.get(run.run_id)
            return current.status if current else terminal_status
        # 提交后扇出只是低延迟优化：终态事件已在同一事务内持久化。
        self.events.notify(envelope)
        return terminal_status

    def _fail_run(self, run: RunRecord, code: str, message: str, *, owner_token: str) -> str:
        # 失败同样是终态转换：状态与 terminal error 事件在同一事务内提交，
        # 且仅当 Run 仍处于本次执行领取的 running 状态、且仍持有执行所有权时才生效，
        # 保证事件流可靠关闭，同时防止失去所有权的旧执行者写终态。
        if self.fail_run_safely(
            run.run_id,
            code=code,
            message=message,
            required_status="running",
            required_owner=owner_token,
        ):
            return "failed"
        current = self.runs.get(run.run_id)
        return current.status if current else "failed"

    def fail_run_safely(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        required_status: str = "running",
        required_owner: str | None = None,
    ) -> bool:
        """把仍处于 ``required_status`` 的 Run 安全终止为 ``failed``（带终态事件）。

        这是异常兜底路径（后台调度器捕获到未处理异常、调度容量不足等）的收口入口：
        绝不允许异常路径留下永久非终态 Run。CAS 保证幂等——重复调用、已被其他路径
        终结的 Run 都不会被覆盖，也不会产生第二条终态事件。

        调度器兜底会带上它派发时使用的 ``required_owner``：已失去所有权的执行者
        不能把「已被重新调度、正在被新执行者运行」的 Run 误判为失败（ADR-035）。
        ``required_status='accepted'`` 的调用方不需要所有权（该状态无执行者）。
        """

        run = self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES or run.status != required_status:
            return False
        return record_terminal_transition(
            pool=self.pool,
            runs=self.runs,
            events=self.events,
            transition=TerminalTransition(
                run_id=run.run_id,
                thread_id=run.thread_id,
                status="failed",
                event_type="error",
                payload={"code": code, "message": message, "terminal": True},
                logical_transition=f"failed:{code}",
                required_status=required_status,
                required_owner=required_owner,
                error_code=code[:64],
                error_message=message[:500],
            ),
        )

    def _authorized_document_ids(self, user_id: str) -> tuple[str, ...]:
        documents, _ = self.documents.list(user_id=user_id, limit=100, cursor=None)
        return tuple(
            document.document_id for document in documents if document.status == "available"
        )
