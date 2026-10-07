"""通过应用 ingestion 路径加载 Demo Knowledge 文档。

自建的 Demo 文档位于 ``demo/knowledge``。上传使用与 API 相同的应用服务，
因此该加载器会走真实的 parser、切分、embedding、向量写入、校验与提升流程
（PROJECT_SPEC §16）。

用法：

    ./scripts/dev-env.ps1 | Out-Null
    uv run python -m scripts.load_demo_knowledge

该加载器需要一个 Demo 用户。当配置的 bootstrap 用户缺失时予以创建，
否则复用（幂等，不覆盖既有凭据）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from scripts.live_defaults import live_credentials_missing
from scripts.verify_live import live_settings

DEMO_KNOWLEDGE_DIR = Path("demo/knowledge")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    settings = live_settings()
    missing = live_credentials_missing()
    if missing:
        print(f"Missing live credentials: {', '.join(missing)}; run scripts/dev-env.ps1.")
        return 2
    if not DEMO_KNOWLEDGE_DIR.exists():
        print(f"No Demo knowledge directory at {DEMO_KNOWLEDGE_DIR}.")
        return 2

    from backend.api.dependencies import build_container

    container = build_container(settings)
    runtime = container.runtime
    user = runtime.auth.get_user_by_username(settings.auth_bootstrap_username)
    if user is None:
        user = runtime.auth.create_user(
            username=settings.auth_bootstrap_username,
            password=settings.auth_bootstrap_password.get_secret_value(),
            display_name=settings.auth_bootstrap_display_name,
        )
        print(f"created Demo user {user.username}")

    loaded = 0
    for path in sorted(DEMO_KNOWLEDGE_DIR.glob("*.md")):
        data = path.read_bytes()
        outcome = runtime.ingestion.submit_upload(
            user_id=user.user_id,
            filename=path.name,
            content_type="text/markdown",
            data=data,
        )
        if outcome.duplicate:
            print(f"  reused {path.name} (job {outcome.ingestion_job_id})")
        else:
            status = runtime.ingestion.process_job(outcome.ingestion_job_id, payload=data)
            print(f"  loaded {path.name}: job {outcome.ingestion_job_id} -> {status}")
            loaded += 1
    total = len(list(DEMO_KNOWLEDGE_DIR.glob("*.md")))
    container.close()
    print(f"Demo knowledge documents processed: {loaded} new, {total} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
