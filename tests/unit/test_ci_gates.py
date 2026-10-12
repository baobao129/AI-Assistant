"""Gate failures must never become a passing release record."""

import xml.etree.ElementTree as ET

import pytest

from scripts import ci_gates


def report(path, tags=(), names=("test_ok",)):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for name in names:
        case = ET.SubElement(suite, "testcase", classname="tests.unit.fixture", name=name)
        for tag in tags:
            ET.SubElement(case, tag)
    ET.ElementTree(root).write(path, encoding="utf-8")


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_junit_rejects_failed_or_skipped_cases(tmp_path, tag):
    path = tmp_path / "report.xml"
    report(path, (tag,))
    result = ci_gates.junit_summary(path, {("tests.unit.fixture", "test_ok")})
    assert result["status"] == "NO-GO"
    assert result[tag]


@pytest.mark.parametrize("mode", ["missing", "empty", "omitted", "duplicate"])
def test_junit_requires_exact_nonempty_collection(tmp_path, mode):
    path = tmp_path / "report.xml"
    if mode != "missing":
        report(
            path,
            names=()
            if mode == "empty"
            else (("test_ok", "test_ok") if mode == "duplicate" else ("test_ok",)),
        )
    expected = {("tests.unit.fixture", "test_ok"), ("tests.unit.fixture", "test_required")}
    with pytest.raises(ValueError):
        ci_gates.junit_summary(path, expected)


def test_junit_passes_only_complete_selection(tmp_path):
    path = tmp_path / "report.xml"
    report(path)
    assert ci_gates.junit_summary(path, {("tests.unit.fixture", "test_ok")})["status"] == "PASS"


def test_redaction_removes_known_and_syntax_secrets():
    result = ci_gates.redact(
        'pw=generated123 token="unknown-value" Bearer abc.def https://alice:pw@example.com',
        {"TEST_MYSQL_PASSWORD": "generated123"},
    )
    assert all(
        value not in result for value in ("generated123", "unknown-value", "abc.def", "alice:pw")
    )


def test_environment_does_not_inherit_provider_or_database(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "do-not-use")
    monkeypatch.setenv("TEST_MYSQL_DATABASE", "production")
    monkeypatch.setenv("RELEASE_COMPOSE_OVERRIDE", "production-compose.yml")
    env = ci_gates.offline_environment()
    assert env["LLM_API_KEY"] == ""
    assert "TEST_MYSQL_DATABASE" not in env
    assert "RELEASE_COMPOSE_OVERRIDE" not in env


def test_fingerprint_excludes_timestamp_and_generated_state(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "module.py").write_text("x = 1")
    first = ci_gates.source_manifest(tmp_path)["fingerprint"]
    (tmp_path / ".env").write_text("private")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "old.json").write_text("{}")
    (tmp_path / "frontend" / "runtime").mkdir(parents=True)
    (tmp_path / "frontend" / "runtime" / "old.json").write_text("{}")
    assert ci_gates.source_manifest(tmp_path)["fingerprint"] == first
    (tmp_path / "backend" / "module.py").write_text("x = 2")
    assert ci_gates.source_manifest(tmp_path)["fingerprint"] != first


def test_cleanup_residual_resource_fails(monkeypatch):
    monkeypatch.setattr(ci_gates, "run", lambda *a, **kw: "")
    monkeypatch.setattr(
        ci_gates,
        "project_inventory",
        lambda _: {
            "containers": [],
            "volumes": ["leftover"],
            "networks": [],
        },
    )
    result = ci_gates.cleanup_projects(
        [["docker", "compose", "--project-name", "ci-gate-12345678"]]
    )
    assert result["status"] == "NO-GO"


def test_cleanup_refuses_unowned_project_before_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(ci_gates, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError):
        ci_gates.cleanup_projects([["docker", "compose", "--project-name", "enterprise-agent"]])
    assert not calls


def test_missing_required_test_stops_before_execution(tmp_path, monkeypatch):
    calls = []

    def collect(*args, **kwargs):
        calls.append(args)
        return 0, "tests/unit/fixture.py::test_ok\n"

    monkeypatch.setattr(ci_gates, "command", collect)
    with pytest.raises(RuntimeError, match="mandatory"):
        ci_gates.pytest_gate("unit", ["tests/unit"], tmp_path, {}, required=("test_required",))
    assert len(calls) == 1


@pytest.mark.parametrize("exit_code,xml", [(1, True), (0, False), (0, True)])
def test_pytest_gate_propagates_execution_failure_skip_or_missing_report(
    tmp_path, monkeypatch, exit_code, xml
):
    def execute(argv, *args, **kwargs):
        if "--collect-only" in argv:
            return 0, "tests/unit/fixture.py::test_ok\n"
        if xml:
            report(tmp_path / "unit.private.xml", tags=("skipped",))
        return exit_code, "bounded failure detail"

    monkeypatch.setattr(ci_gates, "command", execute)
    with pytest.raises(RuntimeError, match="gate failed"):
        ci_gates.pytest_gate("unit", ["tests/unit"], tmp_path, {})
    assert not (tmp_path / "unit.private.xml").exists()


def test_cleanup_failure_still_attempts_other_owned_projects(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "ci-gate-12345678" in argv:
            raise RuntimeError("failed")
        return ""

    monkeypatch.setattr(ci_gates, "run", run)
    result = ci_gates.cleanup_projects(
        [
            ["docker", "compose", "--project-name", "ci-gate-87654321"],
            ["docker", "compose", "--project-name", "ci-gate-12345678"],
        ]
    )
    assert result["status"] == "NO-GO"
    assert any("ci-gate-87654321" in argv for argv in calls)


def test_json_secret_syntax_is_redacted():
    assert "private-value" not in ci_gates.redact('{"token": "private-value"}', {})


def test_artifact_safety_failure_withholds_upload_directory(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "private.env").write_text("secret")
    with pytest.raises(RuntimeError, match="withheld"):
        ci_gates.sanitize_artifacts(artifacts, {})
    assert not artifacts.exists()
    assert (tmp_path / "artifacts-withheld").exists()


def test_artifact_secrets_are_removed_without_destroying_xml(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "unit.xml").write_text(
        "<testsuites><failure>generated-value</failure></testsuites>"
    )
    ci_gates.sanitize_artifacts(artifacts, {"TEST_MYSQL_PASSWORD": "generated-value"})
    assert "generated-value" not in (artifacts / "unit.xml").read_text()
    ET.parse(artifacts / "unit.xml")


@pytest.mark.parametrize("count", [0, 1])
def test_frontend_test_summary_requires_nonempty_complete_pass(count):
    output = f"ℹ tests {count}\nℹ pass 0\nℹ fail 0\nℹ cancelled 0\nℹ skipped {count}\nℹ todo 0"
    with pytest.raises(ValueError):
        ci_gates.frontend_test_summary(output)


def test_frontend_summary_accepts_passing_node_output():
    output = "ℹ tests 20\nℹ pass 20\nℹ fail 0\nℹ cancelled 0\nℹ skipped 0\nℹ todo 0"
    assert ci_gates.frontend_test_summary(output)["tests"] == 20
