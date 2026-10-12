"""Close and restart the actual application process with a persistent Checkpoint."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.live_db
def test_sqlite_resume_after_real_process_restart_preserves_execution(live_settings, tmp_path):
    if not live_settings.mysql_database.startswith("stage1_"):
        pytest.skip("Requires a dedicated stage1_* isolated audit database")
    evidence_root = tmp_path / "restart-worker"
    evidence_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        MYSQL_HOST=live_settings.mysql_host,
        MYSQL_PORT=str(live_settings.mysql_port),
        MYSQL_DATABASE=live_settings.mysql_database,
        MYSQL_USER=live_settings.mysql_user,
        MYSQL_PASSWORD=live_settings.mysql_password.get_secret_value(),
        MYSQL_ROOT_PASSWORD=live_settings.mysql_root_password.get_secret_value(),
        MYSQL_READONLY_USER=live_settings.mysql_readonly_user,
        MYSQL_READONLY_PASSWORD=live_settings.mysql_readonly_password.get_secret_value(),
        MILVUS_URI=live_settings.milvus_uri,
        LLM_PROVIDER="mock",
        SQL_AGENT_PROVIDER="scripted",
        EMBEDDING_PROVIDER="deterministic",
        EMBEDDING_DIMENSION="64",
        RERANK_ENABLED="false",
        AUTH_BOOTSTRAP_PASSWORD=live_settings.auth_bootstrap_password.get_secret_value(),
        CHECKPOINT_MODE="sqlite",
        CHECKPOINT_SQLITE_PATH=str(evidence_root / "checkpoint.sqlite"),
        UPLOAD_ROOT=str(evidence_root / "uploads"),
        LOG_LEVEL="WARNING",
    )
    artifact = evidence_root / "restart.json"
    commands = []
    for phase in ("prepare", "resume"):
        command = [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "tests.integration.audit_restart_worker",
            phase,
            str(artifact),
        ]
        # tests is not a package in all runners; run the worker with repo import path explicitly.
        command = [
            sys.executable,
            "-X",
            "utf8",
            "tests/integration/audit_restart_worker.py",
            phase,
            str(artifact),
        ]
        env["PYTHONPATH"] = os.getcwd()
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
        commands.append({"phase": phase, "exit_code": result.returncode})
        assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(artifact.read_text(encoding="utf-8"))
    assert proof["status"] == "PASS"
    assert proof["prepare_pid"] != proof["resume_pid"]
    evidence = os.environ.get("AUDIT_EVIDENCE_DIR")
    if evidence:
        Path(evidence, "restart-result.json").write_text(
            json.dumps(
                {
                    k: proof[k]
                    for k in (
                        "status",
                        "prepare_pid",
                        "resume_pid",
                        "query_reexecuted",
                        "artifact_count",
                        "terminal_final_count",
                        "sequence_count",
                    )
                }
                | {"commands": commands}
            ),
            encoding="utf-8",
        )
