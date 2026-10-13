"""Offline CI gates: isolated resources, complete test evidence, safe artifact allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from scripts.release_storage import ROOT, compose, configuration, run

SOURCE_DIRS = ("backend", "scripts", "tests", "demo", "frontend", ".github")
SOURCE_FILES = (
    "AGENTS.md",
    "DECISIONS.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
    ".env.example",
)
EXCLUDED = {
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "docs",
    "runtime",
    "artifacts",
    ".git",
    ".venv",
}
SOURCE_EXTENSIONS = {
    ".py",
    ".toml",
    ".lock",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".mjs",
    ".vue",
    ".css",
    ".html",
    ".md",
    ".txt",
    ".conf",
    ".ps1",
    ".sh",
}
SECRET_MARKERS = ("PASSWORD", "API_KEY", "SECRET", "TOKEN", "ACCESS_KEY")
ARTIFACT_FILES = {
    "source-manifest.json",
    "summary.json",
    "ruff-check.json",
    "ruff-format.json",
    "unit.json",
    "unit.xml",
    "nonlive.json",
    "nonlive.xml",
    "integration.json",
    "integration.xml",
    "cursor.json",
    "cursor.xml",
    "smoke.json",
    "smoke.xml",
    "frontend-install.json",
    "frontend-test.json",
    "frontend-build.json",
    "isolation.json",
    "cleanup.json",
    "recovery.json",
    "restart-result.json",
    "provider-call-counters.json",
}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def redact(text: str, environment: Mapping[str, str]) -> str:
    """Redact known secrets plus common credential syntax before writing evidence."""
    values = sorted(
        {v for k, v in environment.items() if v and any(m in k.upper() for m in SECRET_MARKERS)},
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[\w.\-/+=]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"""(?i)((?:password|api[_-]?key|secret|token)["']?\s*[=:]\s*["']?)[^\s,"'<>]+""",
        r"\1[REDACTED]",
        text,
    )
    if any(value in text for value in values):
        raise ValueError("Evidence redaction failed")
    return text


def sanitize_artifacts(directory: Path, environment: Mapping[str, str]) -> None:
    """Fail closed: remove publication eligibility if any artifact is unexpected/unsafe."""
    try:
        for path in directory.iterdir():
            if path.name not in ARTIFACT_FILES or not path.is_file() or path.is_symlink():
                raise ValueError("Unexpected evidence file")
            safe = redact(path.read_text(encoding="utf-8"), environment)
            if path.suffix == ".xml":
                ET.fromstring(safe)
            else:
                json.loads(safe)
            path.write_text(safe, encoding="utf-8")
    except Exception:
        # Preserve local diagnostic data but prevent the workflow allowlist from uploading it.
        directory.rename(directory.with_name("artifacts-withheld"))
        raise RuntimeError("Artifact safety check failed; publication withheld") from None


def offline_environment() -> dict[str, str]:
    """Carry OS/runtime configuration only, never host application/provider settings."""
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "COMPOSE_EXECUTABLE",
        "GITHUB_SHA",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SERVER_URL",
        "GITHUB_REPOSITORY",
        "CI_ARTIFACT_DIR",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
        NO_PROXY="localhost,127.0.0.1",
        LLM_API_KEY="",
        EMBEDDING_API_KEY="",
        MINERU_API_KEY="",
        LANGSMITH_API_KEY="",
    )
    return env


@contextmanager
def process_environment(environment: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def source_manifest(root: Path = ROOT) -> dict:
    files = {root / name for name in SOURCE_FILES if (root / name).is_file()}
    for directory in SOURCE_DIRS:
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(part in EXCLUDED for part in relative.parts)
                and not path.name.startswith(".env")
                and (path.suffix in SOURCE_EXTENSIONS or path.name == "Dockerfile")
            ):
                files.add(path)
    hashes = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    git = {}
    for key, command in (
        ("head", ["git", "rev-parse", "HEAD"]),
        ("status", ["git", "status", "--porcelain=v1", "--untracked-files=all"]),
    ):
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
        git[key] = result.stdout.strip() if result.returncode == 0 else "UNVERIFIED"
    return {"fingerprint": fingerprint, "files": hashes, "git": git}


def snapshot(destination: Path) -> dict:
    manifest = source_manifest()
    destination.mkdir(parents=True, exist_ok=False)
    for name in manifest["files"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    if source_manifest(destination)["fingerprint"] != manifest["fingerprint"]:
        raise RuntimeError("Source changed while copying snapshot")
    write_json(destination.parent / "source-manifest.json", manifest)
    return manifest


def command(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return result.returncode, redact(result.stdout + result.stderr, environment)
    except subprocess.TimeoutExpired:
        return 124, "Command exceeded its bounded timeout; output withheld."


def junit_summary(path: Path, expected: set[tuple[str, str]]) -> dict:
    if not path.is_file():
        raise ValueError("Required JUnit report is missing")
    cases = list(ET.parse(path).getroot().iter("testcase"))
    actual = [(case.get("classname", ""), case.get("name", "")) for case in cases]
    if not cases or len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("JUnit cases do not match the independently collected test selection")
    problems = {
        tag: [f"{c.get('classname')}::{c.get('name')}" for c in cases if c.find(tag) is not None]
        for tag in ("failure", "error", "skipped")
    }
    return {
        "tests": len(cases),
        **problems,
        "status": "NO-GO" if any(problems.values()) else "PASS",
    }


def frontend_test_summary(output: str) -> dict[str, int]:
    counts = {}
    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        match = re.search(rf"(?m)^(?:ℹ\s*|#\s*|\s*){key}\s+(\d+)\s*$", output)
        if not match:
            raise ValueError("Frontend test counters are missing")
        counts[key] = int(match.group(1))
    if (
        not counts["tests"]
        or counts["pass"] != counts["tests"]
        or any(counts[key] for key in ("fail", "cancelled", "skipped", "todo"))
    ):
        raise ValueError("Frontend tests failed, skipped, cancelled, or empty")
    return counts


def pytest_gate(
    name: str,
    selection: list[str],
    work: Path,
    environment: Mapping[str, str],
    *,
    required: tuple[str, ...] = (),
    timeout: int = 900,
) -> dict:
    artifacts = work / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    base = [
        sys.executable,
        "-m",
        "pytest",
        *selection,
        "-p",
        "no:cacheprovider",
        f"--basetemp={work / (name + '-tmp')}",
    ]
    collected_code, collected = command([*base, "--collect-only", "-q"], environment, timeout=120)
    nodes = [
        line.strip()
        for line in collected.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    expected = set()
    for node in nodes:
        parts = node.split("::")
        classname = parts[0][:-3].replace("/", ".")
        if len(parts) > 2:
            classname += "." + ".".join(parts[1:-1])
        expected.add((classname, parts[-1]))
    report = {
        "name": name,
        "commands": [base + ["--collect-only", "-q"]],
        "status": "NO-GO",
        "collected": len(nodes),
        "collection_exit_code": collected_code,
    }
    write_json(artifacts / f"{name}.json", report)
    if collected_code or not nodes or any(not any(item in n for n in nodes) for item in required):
        raise RuntimeError(f"{name}: collection failed or mandatory tests missing")
    raw = work / f"{name}.private.xml"
    argv = [*base, "-ra", f"--junitxml={raw}"]
    code, output = command(argv, environment, timeout=timeout)
    report.update(exit_code=code, commands=[*report["commands"], argv], output=output)
    try:
        if raw.exists():
            safe = artifacts / f"{name}.xml"
            safe.write_text(redact(raw.read_text(encoding="utf-8"), environment), encoding="utf-8")
            report.update(junit_summary(safe, expected))
        else:
            report["reason"] = "JUnit report missing"
    finally:
        raw.unlink(missing_ok=True)
    if code:
        report["status"] = "NO-GO"
    write_json(artifacts / f"{name}.json", report)
    if report["status"] != "PASS":
        raise RuntimeError(f"{name}: test gate failed; see sanitized evidence")
    print(f"{name}: {report['tests']} passed, zero skips", flush=True)
    return report


def project_inventory(project: str) -> dict[str, list[str]]:
    if not re.fullmatch(r"(?:ci-gate|release-drill)-[a-f0-9]{8}(?:-fresh|-restore)?", project):
        raise ValueError("Refuse inventory/cleanup outside invocation-owned projects")
    label = f"label=com.docker.compose.project={project}"
    return {
        "containers": run(["docker", "ps", "-aq", "--filter", label]).split(),
        "volumes": run(["docker", "volume", "ls", "-q", "--filter", label]).split(),
        "networks": run(["docker", "network", "ls", "-q", "--filter", label]).split(),
    }


def cleanup_projects(commands: list[list[str]]) -> dict:
    results = []
    for argv in reversed(commands):
        project = argv[argv.index("--project-name") + 1]
        # Validate ownership before any mutation, not merely before the final query.
        if not re.fullmatch(r"(?:ci-gate|release-drill)-[a-f0-9]{8}(?:-fresh|-restore)?", project):
            raise ValueError("Refuse cleanup outside invocation-owned projects")
        try:
            run([*argv, "down", "--volumes", "--remove-orphans"], timeout=90)
            remaining = project_inventory(project)
            results.append(
                {
                    "project": project,
                    **remaining,
                    "status": "NO-GO" if any(remaining.values()) else "PASS",
                }
            )
        except RuntimeError:
            results.append({"project": project, "status": "NO-GO", "reason": "cleanup failed"})
    return {
        "projects": results,
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "NO-GO",
    }


def initialize_test_database(database: str, environment: dict[str, str]) -> dict[str, str]:
    import pymysql

    from backend.infrastructure.config import Settings
    from backend.infrastructure.schema import initialize_read_only_account, initialize_schema
    from scripts.seed_demo import seed

    if not re.fullmatch(r"(?:stage1|cursor_audit)_[a-f0-9]{8}", database):
        raise ValueError("Only invocation-specific test databases are permitted")
    connection = pymysql.connect(
        host="127.0.0.1",
        port=int(environment["MYSQL_HOST_PORT"]),
        user="root",
        password=environment["MYSQL_ROOT_PASSWORD"],
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW DATABASES LIKE %s", (database,))
            if cursor.fetchone():
                raise ValueError("Refuse to reuse an existing test database")
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            user = environment["MYSQL_USER"]
            if not re.fullmatch(r"[a-z_]+", user):
                raise ValueError("Invalid generated application username")
            cursor.execute(f"GRANT ALL ON `{database}`.* TO '{user}'@'%'")
    finally:
        connection.close()
    env = dict(environment)
    env.update(
        MYSQL_HOST="127.0.0.1",
        MYSQL_PORT=environment["MYSQL_HOST_PORT"],
        MYSQL_DATABASE=database,
        MILVUS_URI=f"http://127.0.0.1:{env['MILVUS_HOST_PORT']}",
        CHECKPOINT_MODE="memory",
        UPLOAD_ROOT=str(Path(env["CI_WORK"]) / database / "uploads"),
    )
    for key in (
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_READONLY_USER",
        "MYSQL_READONLY_PASSWORD",
        "MILVUS_URI",
    ):
        env[f"TEST_{key}"] = env[key]
    env["TEST_AUTH_PASSWORD"] = env["AUTH_BOOTSTRAP_PASSWORD"]
    with process_environment(env):
        settings = Settings(_env_file=None)
        initialize_schema(settings)
        if not initialize_read_only_account(settings, root_password=env["MYSQL_ROOT_PASSWORD"]):
            raise RuntimeError("Dedicated read-only account bootstrap failed")
        seed(settings)
    return env


def live_suites(
    environment: dict[str, str], work: Path, suffix: str, *, focused: bool = True
) -> None:
    if focused:
        from scripts.focused_tests import selection

        env = initialize_test_database(f"stage1_{suffix}", environment)
        env["AUDIT_EVIDENCE_DIR"] = str(work / "artifacts")
        pytest_gate("integration", selection("integration"), work, env)
        return
    for name, database, selection, required in (
        (
            "integration",
            f"stage1_{suffix}",
            ["tests/integration", "--ignore=tests/integration/test_pagination_cursor_live.py"],
            ("test_restart_resume_live.py::", "test_audit_boundaries_live.py::"),
        ),
        (
            "cursor",
            f"cursor_audit_{suffix}",
            ["tests/integration/test_pagination_cursor_live.py"],
            ("test_pagination_cursor_live.py::",),
        ),
    ):
        env = initialize_test_database(database, environment)
        env["AUDIT_EVIDENCE_DIR"] = str(work / "artifacts")
        pytest_gate(name, selection, work, env, required=required)


def live_gate(work: Path, *, focused: bool = True) -> None:
    from dotenv import dotenv_values

    from scripts.verify_release import free_ports, private_environment

    suffix = secrets.token_hex(4)
    project = f"ci-gate-{suffix}"
    private = work / "private.env"
    private_environment(private)
    env = offline_environment()
    env.update({k: v for k, v in dotenv_values(private).items() if v is not None})
    env["CI_WORK"] = str(work)
    for key, port in zip(
        ("MYSQL_HOST_PORT", "MILVUS_HOST_PORT", "BACKEND_PORT", "FRONTEND_PORT"),
        free_ports(),
        strict=True,
    ):
        env[key] = str(port)
    with process_environment(env):
        argv = compose(project, private)
        if any(project_inventory(project).values()):
            raise RuntimeError("Generated project already exists")
        configuration(argv)
        write_json(
            work / "artifacts" / "isolation.json",
            {
                "project": project,
                "ports": {k: v for k, v in env.items() if k.endswith("_PORT")},
                "profile": "offline-deterministic",
                "cleanup": "UNVERIFIED",
            },
        )
        try:
            run([*argv, "up", "-d", "--wait", "--wait-timeout", "300", "mysql", "milvus"])
            live_suites(env, work, suffix, focused=focused)
        finally:
            cleanup = cleanup_projects([argv])
            write_json(work / "artifacts" / "cleanup.json", cleanup)
            if cleanup["status"] != "PASS":
                raise RuntimeError("Isolated cleanup did not pass")
            private.unlink(missing_ok=True)


def ordinary_gate(name: str, work: Path, *, focused: bool = True) -> None:
    env = offline_environment()
    if name == "python":
        for argv in (
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "ruff", "format", "--check", "."],
        ):
            code, output = command(argv, env)
            write_json(
                work / "artifacts" / f"ruff-{argv[3]}.json",
                {"command": argv, "exit_code": code, "output": output},
            )
            if code:
                raise RuntimeError("Static Python gate failed")
        if focused:
            from scripts.focused_tests import selection

            pytest_gate("unit", selection("unit"), work, env)
        else:
            pytest_gate("unit", ["tests/unit"], work, env)
            pytest_gate("nonlive", ["tests/integration", "-m", "not live_db"], work, env)
    else:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise RuntimeError("npm executable is unavailable")
        steps = [("install", ["ci"])]
        if not focused:
            steps.append(("test", ["test"]))
        steps.append(("build", ["run", "build"]))
        for label, arguments in steps:
            argv = [npm, *arguments]
            code, output = command(argv, env, cwd=ROOT / "frontend")
            write_json(
                work / "artifacts" / f"frontend-{label}.json",
                {"command": argv, "exit_code": code, "output": output},
            )
            if code:
                raise RuntimeError(f"Frontend {label} gate failed")
            if label == "test":
                counts = frontend_test_summary(output)
                write_json(
                    work / "artifacts" / "frontend-test.json",
                    {"command": argv, "exit_code": code, "output": output, **counts},
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=("python", "frontend", "live", "recovery", "snapshot"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", choices=("v1-focused", "full"), default="v1-focused")
    args = parser.parse_args()
    work = args.out.resolve()
    if args.gate == "snapshot":
        snapshot(work)
        return
    work.mkdir(parents=True, exist_ok=False)
    artifacts = work / "artifacts"
    artifacts.mkdir()
    manifest = source_manifest()
    write_json(artifacts / "source-manifest.json", manifest)
    now = datetime.now(UTC)
    summary = {
        "gate": args.gate,
        "profile": args.profile,
        "e2e": "NOT_RUN",
        "status": "NO-GO",
        "remote": "UNVERIFIED",
        "v1_release": "NO-GO",
        "fingerprint": manifest["fingerprint"],
        "utc": now.isoformat(),
        "asia_shanghai": now.astimezone(timezone(timedelta(hours=8))).isoformat(),
    }
    if os.environ.get("GITHUB_RUN_ID"):
        summary["remote_execution"] = {
            "sha": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "url": f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        }
    try:
        if args.gate in {"python", "frontend"}:
            ordinary_gate(args.gate, work, focused=args.profile == "v1-focused")
        elif args.gate == "live":
            live_gate(work, focused=args.profile == "v1-focused")
        else:
            env = offline_environment()
            env["CI_ARTIFACT_DIR"] = str(artifacts)
            code, output = command(
                [sys.executable, "-m", "scripts.verify_release", "--recovery-only", "--no-e2e"],
                env,
                timeout=1500,
            )
            summary.update(exit_code=code, output=output)
            if code:
                raise RuntimeError("Recovery gate failed or timed out; cleanup must be verified")
        if source_manifest()["fingerprint"] != manifest["fingerprint"]:
            raise RuntimeError("Source changed during gate execution")
        summary["status"] = "PASS"
    except Exception as error:
        summary["reason"] = f"{type(error).__name__}: {redact(str(error), offline_environment())}"
        raise RuntimeError("CI gate failed; see allowlisted evidence") from None
    finally:
        write_json(artifacts / "summary.json", summary)
        sanitize_artifacts(artifacts, os.environ)


if __name__ == "__main__":
    main()
