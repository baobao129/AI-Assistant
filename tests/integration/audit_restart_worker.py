"""Separate-process worker for the persisted HITL regression; not an API."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from backend.api.dependencies import build_container
from backend.graph.main import build_main_graph
from backend.infrastructure.config import Settings


def main() -> None:
    phase, filename = sys.argv[1:]
    artifact = Path(filename)
    settings = Settings(_env_file=None)
    if not settings.mysql_database.startswith("stage1_"):
        raise RuntimeError("Only the isolated audit DB is supported")
    container = build_container(settings)
    rt = container.runtime
    user = rt.auth.get_user_by_username(settings.auth_bootstrap_username)
    try:
        if phase == "prepare":
            conversation = rt.conversations.create(user_id=user.user_id, title="restart audit")
            handle = container.runs.start_run(
                user_id=user.user_id,
                conversation_id=conversation.conversation_id,
                user_input="export sales amount data to csv",
                thread_id=None,
            )
            assert container.runs.execute(handle.run_id) == "awaiting_approval"
            waiting = rt.runs.get(handle.run_id)
            approval = rt.approvals.get(waiting.approval_request_id)
            assert rt.artifacts.get_by_approval(approval.approval_request_id) is None
            graph = build_main_graph(checkpointer=container.runs.checkpointer, settings=settings)
            config = {"configurable": {"thread_id": handle.thread_id}}
            before = graph.get_state(config).values
            # Make budget preservation non-trivial using the existing internal Checkpoint API.
            # This is test fixture setup, never a client-visible Checkpoint mutation endpoint.
            graph.update_state(
                config,
                {
                    "answer_revision_count": 1,
                    "error": {
                        "code": "",
                        "message": "",
                        "recoverable": False,
                        "repair_attempts": 1,
                        "details": {},
                    },
                },
            )
            before = graph.get_state(config).values
            assert before["tool_results"] and before["messages"]
            decision = container.approvals.decide(
                approval.approval_request_id,
                decision="approve",
                user_id=user.user_id,
                conversation_id=handle.conversation_id,
                thread_id=handle.thread_id,
                run_id=handle.run_id,
            )
            assert decision.successor_run_id
            data = {
                "original_run": handle.run_id,
                "thread": handle.thread_id,
                "approval": approval.approval_request_id,
                "successor": decision.successor_run_id,
                "query_result": before["query_result_id"],
                "results": before["tool_results"],
                "messages": before["messages"],
                "scope": before["document_scope"],
                "revision_count": 1,
                "repair_count": 1,
                "prepare_pid": os.getpid(),
                "queries": rt.application_pool.query_one("SELECT COUNT(*) FROM query_results")[0],
            }
            artifact.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        elif phase == "resume":
            data = json.loads(artifact.read_text(encoding="utf-8"))
            assert data["prepare_pid"] != os.getpid()
            graph = build_main_graph(checkpointer=container.runs.checkpointer, settings=settings)
            config = {"configurable": {"thread_id": data["thread"]}}
            recovered = graph.get_state(config).values
            for key, source in (
                ("tool_results", "results"),
                ("messages", "messages"),
                ("document_scope", "scope"),
                ("query_result_id", "query_result"),
            ):
                assert recovered[key] == data[source], key
            assert recovered["answer_revision_count"] == 1
            assert recovered["error"]["repair_attempts"] == 1
            assert (
                container.runs.resume(data["successor"], approval_request_id=data["approval"])
                == "completed"
            )
            after = graph.get_state(config).values
            assert after["tool_results"][: len(data["results"])] == data["results"]
            assert after["messages"][: len(data["messages"])] == data["messages"]
            assert after["document_scope"] == data["scope"]
            assert after["answer_revision_count"] == 1
            assert (
                rt.application_pool.query_one("SELECT COUNT(*) FROM query_results")[0]
                == data["queries"]
            )
            successor = rt.runs.get(data["successor"])
            assert successor.query_result_id == data["query_result"]
            assert successor.thread_id == data["thread"]
            assert successor.original_run_id == data["original_run"]
            artifact_row = rt.artifacts.get_by_approval(data["approval"])
            assert artifact_row and artifact_row.query_result_id == data["query_result"]
            rows = rt.run_events.list_after(run_id=data["successor"], after_event_id=None)
            assert [row[1] for row in rows] == list(range(1, len(rows) + 1))
            assert sum(row[2] == "final" for row in rows) == 1
            assert sum(row[2] == "error" for row in rows) == 0
            assert (
                container.runs.resume(data["successor"], approval_request_id=data["approval"])
                == "completed"
            )
            assert rt.run_events.list_after(run_id=data["successor"], after_event_id=None) == rows
            assert (
                rt.artifacts.get_by_approval(data["approval"]).artifact_id
                == artifact_row.artifact_id
            )
            data.update(
                resume_pid=os.getpid(),
                status="PASS",
                query_reexecuted=False,
                artifact_count=1,
                terminal_final_count=1,
                sequence_count=len(rows),
            )
            artifact.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        else:
            raise ValueError("Unknown audit worker phase")
    finally:
        container.close()


if __name__ == "__main__":
    main()
