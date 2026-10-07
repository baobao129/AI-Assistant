# 供在线验证运行使用的非机密默认值。
#
# 验收与诊断脚本会读取这些值；真实凭据始终来自 scripts/dev-env.ps1 从 .env
# 导出的环境变量。此处不硬编码任何凭据，也不假设任何默认密码。

import os

LIVE_MYSQL_HOST = "127.0.0.1"
LIVE_MYSQL_PORT = "13306"
LIVE_MYSQL_DATABASE = "enterprise_agent"
LIVE_MYSQL_USER = "agent_app"
LIVE_MYSQL_READONLY_USER = "agent_readonly"
LIVE_MILVUS_URI = "http://127.0.0.1:19530"
LIVE_EMBEDDING_DIMENSION = "64"
LIVE_CHECKPOINT_MODE = "sqlite"
LIVE_CHECKPOINT_PATH = "runtime/live-checkpoints.sqlite"
LIVE_LLM_PROVIDER = "extractive"


def live_credentials_missing() -> list[str]:
    """尚未导出的必需在线凭据名称。"""

    required = (
        "TEST_MYSQL_PASSWORD",
        "TEST_MYSQL_ROOT_PASSWORD",
        "TEST_MYSQL_READONLY_PASSWORD",
        "TEST_AUTH_PASSWORD",
    )
    return [name for name in required if not os.environ.get(name)]
