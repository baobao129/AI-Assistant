"""共享的测试 fixture。

依赖数据库的测试通过 ``TEST_MYSQL_HOST`` / ``TEST_MYSQL_PORT`` 选择性地启用，
以便默认测试套件保持与基础设施解耦；需要对照 Compose 运行时执行这些测试时，
按仓库交接说明中记录的方式导出相应变量即可。
"""

from __future__ import annotations

import os

import pytest

from backend.infrastructure.config import Settings


def pytest_configure(config: pytest.Config) -> None:
    # 注册 live_db marker，供需要真实 MySQL/Milvus 的用例按标记筛选。
    config.addinivalue_line("markers", "live_db: requires the Compose MySQL/Milvus runtime")


@pytest.fixture(autouse=True)
def _isolated_process_env():
    """隔离进程环境，防止依赖的导入副作用泄漏到其他用例。

    pymilvus 在导入时会执行 ``load_dotenv()``，把仓库 ``.env`` 注入 ``os.environ``；
    若不隔离，先运行的集成用例会让后续单元用例看到真实 LLM/Embedding 凭据，
    使「默认值」类断言变成对本地 .env 的隐式依赖（甚至触发真实网络请求）。
    """

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _test_settings() -> Settings:
    # 固定使用 deterministic embedding 与 mock LLM，避免实时测试依赖外部 provider。
    # 同时显式固定内存 Checkpoint：部署档位默认 sqlite（ADR-035）且其路径是**容器内**
    # 路径（/app/runtime/...，经 .env 注入进程环境），宿主直跑测试若继承它会在
    # Windows 上解析成 C:\app\... 而失败；初始化参数优先于环境变量，因此这里显式固定。
    # sqlite 档位本身由 tests/unit/test_checkpoint.py 与 tests/integration/test_graph.py 覆盖。
    return Settings(
        _env_file=None,
        checkpoint_mode="memory",
        mysql_host=os.environ.get("TEST_MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.environ.get("TEST_MYSQL_PORT", "13306")),
        mysql_user=os.environ.get("TEST_MYSQL_USER", "agent_app"),
        mysql_password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
        mysql_root_password=os.environ.get("TEST_MYSQL_ROOT_PASSWORD", ""),
        mysql_readonly_user=os.environ.get("TEST_MYSQL_READONLY_USER", "agent_readonly"),
        mysql_readonly_password=os.environ.get("TEST_MYSQL_READONLY_PASSWORD", ""),
        mysql_database=os.environ.get("TEST_MYSQL_DATABASE", "enterprise_agent"),
        milvus_uri=os.environ.get("TEST_MILVUS_URI", "http://127.0.0.1:19530"),
        embedding_provider="deterministic",
        embedding_dimension=64,
        parser_provider="lightweight",
        llm_provider="mock",
        auth_bootstrap_username="demo",
        auth_bootstrap_password=os.environ.get("TEST_AUTH_PASSWORD", "test-bootstrap"),
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    """面向数据库测试的 Settings；未导出测试凭据时直接跳过用例。"""

    # 缺少任一数据库口令都视为「未配置实时环境」，跳过而非失败，
    # 这样默认套件在没有 Compose 运行时也能全绿。
    if not os.environ.get("TEST_MYSQL_PASSWORD"):
        pytest.skip("TEST_MYSQL_PASSWORD is not exported; live database tests are skipped.")
    if not os.environ.get("TEST_MYSQL_READONLY_PASSWORD"):
        pytest.skip("TEST_MYSQL_READONLY_PASSWORD is not exported; live tests are skipped.")
    return _test_settings()
