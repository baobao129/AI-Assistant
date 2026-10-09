"""Demo loader 的安全门控测试（Final Review MAJOR 2）。

加载器绝不允许用空密码创建账号：用户名或密码缺失时必须安全失败（非零退出码），
并且在失败路径上不得构建应用容器（即不产生任何数据库副作用）。
"""

from __future__ import annotations

from pydantic import SecretStr

import scripts.load_demo_knowledge as loader


class StubSettings:
    def __init__(self, *, username: str, password: str) -> None:
        self.auth_bootstrap_username = username
        self.auth_bootstrap_password = SecretStr(password)


def _record_container_builds(monkeypatch) -> list[object]:
    built: list[object] = []

    def fake_build(settings):
        built.append(settings)
        return object()

    monkeypatch.setattr("backend.api.dependencies.build_container", fake_build)
    return built


def test_empty_password_fails_safely_without_building_anything(monkeypatch):
    built = _record_container_builds(monkeypatch)
    monkeypatch.setattr(loader, "Settings", lambda: StubSettings(username="demo", password=""))

    assert loader.main() == 2
    assert built == []


def test_missing_username_fails_safely_without_building_anything(monkeypatch):
    built = _record_container_builds(monkeypatch)
    monkeypatch.setattr(loader, "Settings", lambda: StubSettings(username="   ", password="secret"))

    assert loader.main() == 2
    assert built == []


def test_missing_demo_directory_fails_safely(monkeypatch, tmp_path):
    built = _record_container_builds(monkeypatch)
    monkeypatch.setattr(
        loader, "Settings", lambda: StubSettings(username="demo", password="secret")
    )
    monkeypatch.setattr(loader, "DEMO_KNOWLEDGE_DIR", tmp_path / "missing")

    assert loader.main() == 2
    assert built == []
