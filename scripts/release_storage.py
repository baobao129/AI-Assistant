"""Offline Compose volume snapshots; credentials are supplied outside the archive."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

VOLUMES = ("mysql_data", "etcd_data", "minio_data", "milvus_data", "checkpoint_data")
HELPER = "python:3.12.13-slim"
ROOT = Path(__file__).resolve().parents[1]


def run(
    arguments: list[str], *, environment: dict[str, str] | None = None, timeout: int = 600
) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(arguments[0]).name} operation timed out") from None
    if result.returncode:
        # Docker/Compose diagnostics can include resolved credentials.
        raise RuntimeError(f"{Path(arguments[0]).name} operation failed (exit {result.returncode})")
    return result.stdout


def compose(project: str, env_file: Path) -> list[str]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,60}", project):
        raise ValueError("Use an explicit lowercase Compose project name")
    command = [
        os.environ.get("COMPOSE_EXECUTABLE", "docker"),
        *([] if os.environ.get("COMPOSE_EXECUTABLE") else ["compose"]),
        "--project-name",
        project,
        "--env-file",
        str(env_file.resolve()),
        "--file",
        str(ROOT / "compose.yaml"),
    ]
    if override := os.environ.get("RELEASE_COMPOSE_OVERRIDE"):
        command.extend(["--file", str(Path(override).resolve())])
    return command


def configuration(command: list[str]) -> dict:
    config = json.loads(run([*command, "config", "--format", "json"]))
    expected = {f"{config['name']}_{name}" for name in VOLUMES}
    actual = {config["volumes"][name]["name"] for name in VOLUMES}
    if actual != expected:
        raise ValueError("Only project-scoped volumes are supported")
    return config


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def backup(project: str, env_file: Path, destination: Path) -> None:
    command = compose(project, env_file)
    config = configuration(command)
    for name in VOLUMES:
        volume = f"{project}_{name}"
        run(["docker", "volume", "inspect", volume])
        if run(["docker", "ps", "-q", "--filter", f"volume={volume}"]).strip():
            raise ValueError("Stop every writer/container using the source volumes first")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {
        "format": 1,
        "consistency": "all-volume-consumers-stopped",
        "source_project": project,
        "images": {k: v["image"] for k, v in config["services"].items() if "image" in v},
        "helper_image": HELPER,
        "volumes": {},
    }
    for name in VOLUMES:
        archive = f"{name}.tar.gz"
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={project}_{name},dst=/source,readonly",
                "--mount",
                f"type=bind,src={destination},dst=/backup",
                HELPER,
                "tar",
                "--exclude=.env",
                "--exclude=.env.*",
                "-czpf",
                f"/backup/{archive}",
                "-C",
                "/source",
                ".",
            ]
        )
        manifest["volumes"][name] = {"file": archive, "sha256": digest(destination / archive)}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("backup_verified: five stopped volumes; no environment file included")


def restore(project: str, env_file: Path, source: Path) -> None:
    command = compose(project, env_file)
    config = configuration(command)
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("consistency") != "all-volume-consumers-stopped":
        raise ValueError("Unsupported or inconsistent snapshot")
    if set(manifest["volumes"]) != set(VOLUMES):
        raise ValueError("Incomplete snapshot")
    images = {k: v["image"] for k, v in config["services"].items() if "image" in v}
    if images != manifest["images"]:
        raise ValueError("Restore must use the original service image versions")
    if run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]):
        raise ValueError("Restore requires a new project with no containers")
    existing = set(run(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines())
    if any(f"{project}_{name}" in existing for name in VOLUMES):
        raise ValueError("Restore refuses every existing destination volume")
    # Validate every archive before creating any destination volume.
    for name in VOLUMES:
        entry = manifest["volumes"][name]
        if entry["file"] != f"{name}.tar.gz" or digest(source / entry["file"]) != entry["sha256"]:
            raise ValueError("Snapshot checksum/file validation failed")
        members = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=bind,src={source},dst=/backup,readonly",
                HELPER,
                "tar",
                "-tzf",
                f"/backup/{entry['file']}",
            ]
        )
        if any(item.startswith("/") or ".." in Path(item).parts for item in members.splitlines()):
            raise ValueError("Unsafe archive member")
    for name in VOLUMES:
        volume = f"{project}_{name}"
        run(
            [
                "docker",
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={project}",
                "--label",
                f"com.docker.compose.volume={name}",
                volume,
            ]
        )
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--mount",
                f"type=volume,src={volume},dst=/target",
                "--mount",
                f"type=bind,src={source},dst=/backup,readonly",
                HELPER,
                "tar",
                "-xzpf",
                f"/backup/{name}.tar.gz",
                "-C",
                "/target",
            ]
        )
    print("restore_verified: five new volumes; start separately with original credentials")
