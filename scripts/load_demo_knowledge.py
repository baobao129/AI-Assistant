"""通过应用 ingestion 路径加载 Demo Knowledge 文档。

自建的 Demo 文档位于 ``demo/knowledge``。上传使用与 API 相同的应用服务，
因此该加载器会走真实的 parser、切分、embedding、向量写入、校验与提升流程
（PROJECT_SPEC §16）。

用法（与 backend 相同的环境，见 README）：

    docker compose exec backend python -m scripts.load_demo_knowledge

宿主侧先加载开发环境脚本（它把 MySQL/Milvus 指向 Compose 发布端口并选择离线 provider）：

    . ./scripts/dev-env.ps1
    uv run python -m scripts.load_demo_knowledge

配置来源与 backend 一致（``Settings``：容器内读容器环境，宿主读 ``.env`` 与已导出的
环境变量）。该加载器需要一个 Demo 用户：复用既有的安全入口
``AuthService.bootstrap_demo_user()``，仅在用户缺失时创建，绝不覆盖既有凭据，
也绝不允许用空密码创建账号。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from backend.infrastructure.config import Settings

DEMO_KNOWLEDGE_DIR = Path("demo/knowledge")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()

    # 安全门控：绝不创建空密码账号。用户名或密码缺失都直接失败并返回非零退出码。
    username = settings.auth_bootstrap_username.strip()
    password = settings.auth_bootstrap_password.get_secret_value()
    if not username or not password:
        print(
            "AUTH_BOOTSTRAP_USERNAME / AUTH_BOOTSTRAP_PASSWORD must be configured with "
            "non-empty values; refusing to load Demo knowledge."
        )
        return 2
    if not DEMO_KNOWLEDGE_DIR.exists():
        print(f"No Demo knowledge directory at {DEMO_KNOWLEDGE_DIR}.")
        return 2

    from backend.api.dependencies import build_container

    container = build_container(settings)
    runtime = container.runtime
    # 复用既有的安全入口：只在用户缺失时创建，不覆盖既有账号，空凭据会被其自身拒绝。
    container.auth.bootstrap_demo_user()
    user = runtime.auth.get_user_by_username(username)
    if user is None:
        print("Demo user is unavailable; check the AUTH_BOOTSTRAP_* configuration.")
        container.close()
        return 2

    loaded = 0
    for path in sorted(DEMO_KNOWLEDGE_DIR.glob("*.md")):
        data = path.read_bytes()
        # 与 API 上传一致：先把原文件写入受控存储，再登记 Document/Job。
        stored = runtime.upload_storage.store_bytes(data)
        outcome = runtime.ingestion.submit_upload(
            user_id=user.user_id,
            filename=path.name,
            content_type="text/markdown",
            stored=stored,
        )
        if outcome.duplicate:
            print(f"  reused {path.name} (job {outcome.ingestion_job_id})")
        else:
            status = runtime.ingestion.process_job(outcome.ingestion_job_id)
            print(f"  loaded {path.name}: job {outcome.ingestion_job_id} -> {status}")
            loaded += 1
    total = len(list(DEMO_KNOWLEDGE_DIR.glob("*.md")))
    container.close()
    print(f"Demo knowledge documents processed: {loaded} new, {total} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
