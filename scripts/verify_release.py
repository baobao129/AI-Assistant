"""Fresh-container and offline restore drill using only newly created projects."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from scripts.release_storage import (
    HELPER,
    ROOT,
    VOLUMES,
    backup,
    compose,
    configuration,
    restore,
    run,
)


def free_ports() -> list[int]:
    sockets = [socket.socket() for _ in range(4)]
    try:
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def private_environment(path: Path) -> None:
    if path.exists():
        raise ValueError("Refusing to overwrite an existing private environment")
    values = {
        key: secrets.token_hex(24)
        for key in (
            "MYSQL_ROOT_PASSWORD",
            "MYSQL_PASSWORD",
            "MYSQL_READONLY_PASSWORD",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "AUTH_BOOTSTRAP_PASSWORD",
        )
    }
    values.update(
        {
            "MYSQL_DATABASE": "enterprise_agent",
            "MYSQL_USER": "agent_app",
            "MYSQL_READONLY_USER": "agent_readonly",
            "AUTH_BOOTSTRAP_USERNAME": "demo",
            "UPLOAD_ROOT": "runtime/uploads",
            "DATA_SOURCE_ID": "demo_sales",
            "LLM_PROVIDER": "extractive",
            "SQL_AGENT_PROVIDER": "auto",
            "EMBEDDING_PROVIDER": "deterministic",
            "EMBEDDING_DIMENSION": "64",
            "RERANK_ENABLED": "false",
            "PARSER_PROVIDER": "lightweight",
            "CHECKPOINT_MODE": "sqlite",
            "LANGSMITH_TRACING": "false",
            "LLM_API_KEY": "",
            "EMBEDDING_API_KEY": "",
            "MINERU_API_KEY": "",
        }
    )
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")


def wait_run(client: httpx.Client, run_id: str, target: str) -> dict:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/runs/{run_id}").json()
        if state["status"] == target:
            return state
        if state["status"] in {"failed", "rejected", "expired"}:
            raise RuntimeError("Drill execution reached an unexpected terminal state")
        time.sleep(0.25)
    raise RuntimeError("Drill execution timed out")


@contextmanager
def login(port: int, env_file: Path) -> Iterator[httpx.Client]:
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=90)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "demo",
            "password": values["AUTH_BOOTSTRAP_PASSWORD"],
        },
    )
    response.raise_for_status()
    client.headers["Authorization"] = f"Bearer {response.json()['session']['token']}"
    try:
        yield client
    finally:
        client.close()


def export_run(client: httpx.Client, conversation: str) -> tuple[str, dict, dict]:
    response = client.post(
        f"/api/v1/conversations/{conversation}/runs",
        json={
            "user_input": "export sales amount data to csv",
        },
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    state = wait_run(client, run_id, "awaiting_approval")
    events = [
        json.loads(line[6:])
        for line in client.get(f"/api/v1/runs/{run_id}/events").text.splitlines()
        if line.startswith("data: ")
    ]
    approval = next(
        event["payload"] for event in events if event["event_type"] == "approval_required"
    )
    return run_id, state, approval


def decide(
    client: httpx.Client, conversation: str, run_id: str, state: dict, approval: dict
) -> str:
    response = client.post(
        f"/api/v1/approvals/{approval['approval_request_id']}/approve",
        json={
            "conversation_id": conversation,
            "thread_id": state["thread_id"],
            "run_id": run_id,
        },
    )
    response.raise_for_status()
    successor = response.json()["successor_run_id"]
    wait_run(client, successor, "completed")
    return successor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recovery-only", action="store_true")
    args = parser.parse_args()
    from scripts.ci_gates import (
        cleanup_projects,
        live_suites,
        offline_environment,
        process_environment,
        project_inventory,
        pytest_gate,
        write_json,
    )

    # In a dedicated subprocess: discard all host application/provider configuration.
    clean = offline_environment()
    os.environ.clear()
    os.environ.update(clean)
    suffix = secrets.token_hex(4)
    projects = [f"release-drill-{suffix}-fresh", f"release-drill-{suffix}-restore"]
    work = ROOT / "runtime" / projects[0]
    work.mkdir(parents=True, exist_ok=False)
    env_file = work / "private.env"
    private_environment(env_file)
    override = work / "compose.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "backend": {"image": f"release-drill-backend:{suffix}"},
                    "frontend": {"image": f"release-drill-frontend:{suffix}"},
                }
            }
        ),
        encoding="utf-8",
    )
    os.environ["RELEASE_COMPOSE_OVERRIDE"] = str(override)
    # Empty provider keys and runtime profiles override any host credentials.
    for line in env_file.read_text().splitlines():
        key, value = line.split("=", 1)
        os.environ[key] = value
    evidence = {
        "projects": projects,
        "checks": [],
        "profile": "offline-deterministic",
        "result": "failed",
    }
    commands = [compose(project, env_file) for project in projects]
    owned_commands = []
    try:
        ports = free_ports()
        for key, value in zip(
            ("MYSQL_HOST_PORT", "MILVUS_HOST_PORT", "BACKEND_PORT", "FRONTEND_PORT"),
            ports,
            strict=True,
        ):
            os.environ[key] = str(value)
        for command in commands:
            configuration(command)
            if any(project_inventory(command[command.index("--project-name") + 1]).values()):
                raise RuntimeError("Generated recovery project already exists")
        owned_commands.extend(commands)
        print("fresh_start: isolated containers and newly allocated volumes", flush=True)
        run([*commands[0], "up", "-d", "--build", "--wait", "--wait-timeout", "300"])
        evidence["checks"].append("fresh_all_six_services_healthy")
        try:
            backup(projects[0], env_file, work / "must-not-create")
        except ValueError as error:
            assert "Stop every writer" in str(error)
        else:
            raise AssertionError("Active-volume backup should be rejected")
        assert not (work / "must-not-create").exists()
        evidence["checks"].append("backup_refuses_active_volume")
        run([*commands[0], "exec", "-T", "backend", "python", "-m", "scripts.seed_demo"])
        run([*commands[0], "exec", "-T", "backend", "python", "-m", "scripts.load_demo_knowledge"])
        # Re-run existing idempotent expand migrations against the initialized database.
        run(
            [
                *commands[0],
                "exec",
                "-T",
                "backend",
                "python",
                "-c",
                "from backend.infrastructure.config import Settings; "
                "from backend.infrastructure.schema import initialize_schema; "
                "initialize_schema(Settings()); initialize_schema(Settings())",
            ]
        )
        evidence["checks"].append("schema_initialize_twice")
        with login(ports[2], env_file) as client:
            documents = client.get("/api/v1/documents").json()["items"]
            assert len(documents) == 2 and all(d["status"] == "available" for d in documents)
            conversation = client.post(
                "/api/v1/conversations", json={"title": "restore-drill"}
            ).json()["conversation_id"]
            completed = export_run(client, conversation)
            decide(client, conversation, *completed)
            pending = export_run(client, conversation)
            evidence["checks"].append("approved_export_and_waiting_checkpoint_created")
        run([*commands[0], "stop", "--timeout", "60"])
        backup(projects[0], env_file, work / "snapshot")
        try:
            restore(projects[0], env_file, work / "snapshot")
        except ValueError as error:
            assert "new project" in str(error)
        else:
            raise AssertionError("Existing-target restore should be rejected")
        manifest_path = work / "snapshot" / "manifest.json"
        original_manifest = manifest_path.read_text(encoding="utf-8")
        corrupt_manifest = json.loads(original_manifest)
        corrupt_manifest["volumes"]["mysql_data"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(corrupt_manifest), encoding="utf-8")
        try:
            restore(projects[1], env_file, work / "snapshot")
        except ValueError as error:
            assert "checksum" in str(error)
        else:
            raise AssertionError("Corrupt snapshot should be rejected")
        finally:
            manifest_path.write_text(original_manifest, encoding="utf-8")
        existing_volumes = run(["docker", "volume", "ls", "--format", "{{.Name}}"])
        assert all(f"{projects[1]}_{name}" not in existing_volumes for name in VOLUMES)
        evidence["checks"].extend(
            ["restore_refuses_existing_target", "corruption_rejected_before_write"]
        )
        restore(projects[1], env_file, work / "snapshot")
        run([*commands[1], "up", "-d", "--no-build", "--wait", "--wait-timeout", "300"])
        with login(ports[2], env_file) as client:
            restored_documents = client.get("/api/v1/documents").json()["items"]
            assert {d["document_id"] for d in restored_documents} == {
                d["document_id"] for d in documents
            }
            decide(client, conversation, *pending)
            knowledge = client.post(
                f"/api/v1/conversations/{conversation}/runs",
                json={
                    "user_input": "What does the travel policy document say about flights?",
                    "document_ids": [
                        next(d["document_id"] for d in documents if "travel" in d["source_name"])
                    ],
                },
            ).json()
            wait_run(client, knowledge["run_id"], "completed")
            stream = client.get(f"/api/v1/runs/{knowledge['run_id']}/events").text
            assert '"event_type":"retrieval"' in stream.replace(" ", "")
            evidence["checks"].extend(
                [
                    "restored_login_documents",
                    "restored_checkpoint_resume",
                    "restored_milvus_scoped_retrieval",
                ]
            )
        exported = run(
            [
                *commands[1],
                "exec",
                "-T",
                "mysql",
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_PASSWORD" mysql -u"$MYSQL_USER" '
                '-D"$MYSQL_DATABASE" -N -e "SELECT COUNT(*) FROM export_artifacts"',
            ]
        )
        assert int(exported.strip()) == 2
        evidence["checks"].append("restored_original_export_plus_resumed_export")
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={projects[1]}_checkpoint_data,dst=/runtime,readonly",
                HELPER,
                "python",
                "-c",
                "import sqlite3,pathlib; "
                "assert list(pathlib.Path('/runtime/uploads').rglob('*')); "
                "c=sqlite3.connect('file:/runtime/checkpoints.sqlite?mode=ro',uri=True); "
                "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
                "assert c.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]>0",
            ]
        )
        evidence["checks"].append("restored_uploads_and_sqlite_integrity")
        smoke_environment = dict(os.environ)
        smoke_environment.update(
            SMOKE_BASE_URL=f"http://127.0.0.1:{ports[3]}",
            SMOKE_USERNAME="demo",
            SMOKE_PASSWORD=os.environ["AUTH_BOOTSTRAP_PASSWORD"],
        )
        pytest_gate("smoke", ["tests/e2e", "-m", "smoke"], work, smoke_environment)
        evidence["checks"].append("restored_http_smoke")
        # Persist completed recovery evidence even if a separate regression gate fails.
        evidence["recovery_result"] = "passed"
        if not args.recovery_only:
            # Tests own maintenance timing; stop writers before dedicated DB suites.
            run([*commands[1], "stop", "frontend", "backend"])
            integration_environment = dict(os.environ)
            integration_environment["CI_WORK"] = str(work)
            live_suites(integration_environment, work, suffix)
            evidence["checks"].append("isolated_complete_integration_suite")
        evidence["mode"] = "recovery-only" if args.recovery_only else "complete"
        evidence["result"] = "passed"
        print(json.dumps(evidence, indent=2))
    finally:
        # Only names generated in this invocation are eligible for volume removal.
        with process_environment(dict(os.environ)):
            cleanup = cleanup_projects(owned_commands)
        evidence["cleanup"] = cleanup
        evidence["isolated_projects_removed"] = cleanup["status"] == "PASS"
        if cleanup["status"] != "PASS":
            evidence["result"] = "failed"
        write_json(work / "report.json", evidence)
        if artifact_dir := os.environ.get("CI_ARTIFACT_DIR"):
            artifact_root = Path(artifact_dir)
            write_json(artifact_root / "recovery.json", evidence)
            # Only these sanitized smoke files are eligible for publication.
            for name in ("smoke.json", "smoke.xml"):
                source = work / "artifacts" / name
                if source.is_file():
                    (artifact_root / name).write_bytes(source.read_bytes())
        if cleanup["status"] != "PASS":
            raise RuntimeError(
                "Isolated cleanup incomplete; private environment retained for recovery"
            )
        env_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
