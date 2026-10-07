"""Chat, Run lifecycle, Graph execution, and SSE event mapping (API-004 / API-005).

The Graph owns execution; this service owns Graph State construction, Tool
execution scope, Conversation Persistence, Run status, and Application Events.
It never exposes raw Graph State, Checkpoint data, or hidden reasoning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.application.events import RunEventPublisher
from backend.domain.errors import AppError, conflict, not_found
from backend.domain.state import GraphState, initial_state
from backend.graph.main import build_main_graph
from backend.infrastructure.config import Settings
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

    # Request handling ------------------------------------------------------
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

    # Execution -------------------------------------------------------------
    def execute(self, run_id: str) -> str:
        run = self.runs.get(run_id)
        if run is None:
            return "failed"
        if run.status in TERMINAL_STATUSES:
            return run.status
        self.runs.update_status(run_id, "running")
        self.events.publish(
            run_id=run_id,
            thread_id=run.thread_id,
            event_type="agent_start",
            payload={"status": "running"},
        )
        state = initial_state(
            conversation_id=run.conversation_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            user_input=run.user_input,
        )
        return self._run_graph(run=run, state=state, approval_request_id=None)

    def resume(self, run_id: str, *, approval_request_id: str | None = None) -> str:
        """Continue an interrupted Run under the same Thread and Checkpoint lineage."""

        run = self.runs.get(run_id)
        if run is None:
            return "failed"
        state = initial_state(
            conversation_id=run.conversation_id,
            thread_id=run.thread_id,
            run_id=run.run_id,
            user_input=run.user_input,
        )
        bound_query_result_id: str | None = None
        if approval_request_id:
            approval = self.approvals.get(approval_request_id)
            if approval is not None and approval.status == "approved":
                state["approval_request_id"] = approval_request_id
                state["interrupt_status"] = "approved"
                # Approval binds one immutable snapshot; resume reuses the exact one.
                bound_query_result_id = str(approval.binding.get("query_result_id") or "") or None
                state["query_result_id"] = bound_query_result_id
        return self._run_graph(
            run=run,
            state=state,
            approval_request_id=approval_request_id,
            bound_query_result_id=bound_query_result_id,
        )

    def _run_graph(
        self,
        *,
        run: RunRecord,
        state: GraphState,
        approval_request_id: str | None,
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
        try:
            result = self._stream_graph(graph=graph, state=state, config=config, run=run)
        except Exception as error:  # noqa: BLE001 - Graph failure becomes a safe terminal error
            logger.warning(
                "graph_execution_failed: %s: %s",
                type(error).__name__,
                str(error)[:300],
            )
            self._fail_run(run, "internal_error", "The request could not complete safely.")
            return "failed"
        return self._finalize_run(run, result)

    def _stream_graph(
        self, *, graph: Any, state: GraphState, config: dict, run: RunRecord
    ) -> GraphState:
        """Stream State snapshots so Application Events follow execution order."""

        latest: GraphState = state
        seen_intents: set[str] = set()
        seen_retrievals = 0
        seen_calls = 0
        seen_results = 0
        seen_citations = 0
        for snapshot in graph.stream(state, config, stream_mode="values"):
            latest = snapshot
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
                )
            calls = [call for call in (snapshot.get("messages") or []) if call.get("tool_calls")]
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
                    )
            seen_calls = len(calls)
            tool_results = snapshot.get("tool_results") or []
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
                )
            seen_results = len(tool_results)
            citations = snapshot.get("citations") or []
            for citation in citations[seen_citations:]:
                self.events.publish(
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    event_type="citation",
                    payload=citation,
                )
            seen_citations = len(citations)
        return latest

    def _finalize_run(self, run: RunRecord, state: GraphState) -> str:
        approval_request_id = state.get("approval_request_id")
        if approval_request_id and state.get("interrupt_status") == "pending":
            # HITL: the waiting Run stays awaiting_approval and is not business-complete.
            self.runs.update_status(
                run.run_id,
                "awaiting_approval",
                routing_outcome=state.get("routing_outcome"),
                intent=state.get("intent"),
                approval_request_id=approval_request_id,
            )
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
                    "summary": (state.get("tool_results") or [{}])[-1]
                    .get("data", {})
                    .get("summary"),
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
            )
        self.runs.update_status(
            run.run_id,
            terminal_status,
            routing_outcome=state.get("routing_outcome"),
            intent=state.get("intent"),
            final_response=final_response,
            query_result_id=state.get("query_result_id"),
            terminal=True,
        )
        self.conversations.add_message(
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
                    {
                        "tool_id": result.get("tool_id"),
                        "status": result.get("status"),
                    }
                    for result in (state.get("tool_results") or [])
                ],
            },
        )
        self.conversations.touch(run.conversation_id)
        self.events.publish(
            run_id=run.run_id,
            thread_id=run.thread_id,
            event_type="final",
            payload={"status": terminal_status, "content": final_response},
        )
        return terminal_status

    def _fail_run(self, run: RunRecord, code: str, message: str) -> str:
        self.runs.update_status(
            run.run_id,
            "failed",
            error_code=code[:64],
            error_message=message[:500],
            terminal=True,
        )
        self.events.publish(
            run_id=run.run_id,
            thread_id=run.thread_id,
            event_type="error",
            payload={"code": code, "message": message, "terminal": True},
        )
        return "failed"

    def _authorized_document_ids(self, user_id: str) -> tuple[str, ...]:
        documents, _ = self.documents.list(user_id=user_id, limit=100, cursor=None)
        return tuple(
            document.document_id for document in documents if document.status == "available"
        )
