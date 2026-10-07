# Non-secret defaults for live verification runs.
#
# Acceptance and diagnostic scripts read these values; the real credentials always come
# from the environment that scripts/dev-env.ps1 exports from .env. No credential is
# hard-coded here, and no default password is assumed.

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
    """Names of required live credentials that are not exported."""

    required = (
        "TEST_MYSQL_PASSWORD",
        "TEST_MYSQL_ROOT_PASSWORD",
        "TEST_MYSQL_READONLY_PASSWORD",
        "TEST_AUTH_PASSWORD",
    )
    return [name for name in required if not os.environ.get(name)]
