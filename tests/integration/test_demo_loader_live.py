"""Demo loader 的实时行为检查（Final Review MAJOR 2 的有效密码/已有用户场景）。

针对已配置的应用数据库运行；未导出实时凭据时会被跳过（见 ``tests/conftest.py``）。
验证：

- 配置了有效密码时，加载器可以幂等地创建/复用 Demo 用户；
- 账号必须能用配置的密码登录（证明它不是空密码账号）；
- 第二次运行复用既有用户，不覆盖凭据。
"""

from __future__ import annotations

import pytest

import scripts.load_demo_knowledge as loader


@pytest.mark.live_db
def test_loader_creates_then_reuses_the_demo_user_with_a_non_empty_password(
    live_settings, monkeypatch
):
    monkeypatch.setattr(loader, "Settings", lambda: live_settings)

    # 第一次运行：创建（或复用）Demo 用户并处理 Demo 语料。
    assert loader.main() == 0

    from backend.api.dependencies import build_container

    container = build_container(live_settings)
    try:
        user = container.runtime.auth.get_user_by_username(live_settings.auth_bootstrap_username)
        assert user is not None
        # 账号必须能用配置的密码登录：空密码账号会在这里暴露。
        login = container.auth.login(
            username=live_settings.auth_bootstrap_username,
            password=live_settings.auth_bootstrap_password.get_secret_value(),
        )
        assert login.user.user_id == user.user_id
        assert live_settings.auth_bootstrap_password.get_secret_value()
    finally:
        container.close()

    # 第二次运行：幂等复用既有用户与语料，不覆盖凭据。
    assert loader.main() == 0

    container = build_container(live_settings)
    try:
        again = container.auth.login(
            username=live_settings.auth_bootstrap_username,
            password=live_settings.auth_bootstrap_password.get_secret_value(),
        )
        assert again.user.user_id == user.user_id
    finally:
        container.close()
