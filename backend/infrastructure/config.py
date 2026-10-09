from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """类型化运行时配置；此处绝不硬编码密钥。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # 运行时
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    initialize_schema_on_startup: bool = True

    # MySQL 应用数据
    mysql_host: str = "mysql"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "enterprise_agent"
    mysql_user: str = "agent_app"
    mysql_password: SecretStr = SecretStr("")
    mysql_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    mysql_pool_size: int = Field(default=5, ge=1, le=20)

    # 仅供 SQL Agent 查询路径使用的 MySQL 只读账号；它与应用账号指向同一台
    # 服务器，区别只在凭据。
    mysql_readonly_user: str = "agent_readonly"
    mysql_readonly_password: SecretStr = SecretStr("")
    mysql_readonly_pool_size: int = Field(default=3, ge=1, le=10)
    # 仅用于一次性引导创建只读账号的管理凭据；绝不写入日志。
    mysql_root_password: SecretStr = SecretStr("")

    # 向量存储
    milvus_uri: str = "http://milvus:19530"
    milvus_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Checkpoint
    checkpoint_mode: Literal["memory", "sqlite"] = "memory"
    checkpoint_sqlite_path: Path = Path("runtime/checkpoints.sqlite")

    # 有界执行预算
    max_tool_iterations: int = Field(default=8, ge=1, le=8)
    max_repair_attempts: int = Field(default=3, ge=0, le=3)
    max_answer_revisions: int = Field(default=1, ge=0, le=1)

    # LLM
    llm_provider: Literal["remote", "local", "mock", "extractive"] = "remote"
    llm_base_url: str = "https://api.openai.com/v1"
    local_llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_max_input_chars: int = Field(default=8000, ge=100, le=100000)
    local_llm_streaming: bool = False
    local_llm_structured_output: bool = False
    local_llm_tool_calling: bool = False
    # 结构化数据 Agent 的模型选择："auto" 跟随 LLM provider，并在 mock/extractive
    # 开发档位下使用确定性的 agent。
    sql_agent_provider: Literal["auto", "llm", "scripted"] = "auto"

    # Embedding provider（SiliconFlow 是 MVP 的远程 provider）
    embedding_provider: Literal["siliconflow", "deterministic"] = "siliconflow"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = Field(default=1024, ge=8, le=4096)
    embedding_api_key: SecretStr = SecretStr("")
    embedding_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    embedding_batch_size: int = Field(default=32, ge=1, le=128)

    # Parser
    parser_provider: Literal["lightweight", "mineru"] = "lightweight"
    # mineru.net v4 云解析 API 的基地址（适配器在其上拼接 /file-urls/batch 与
    # /extract-results/batch/{batch_id}）；本地自托管 MinerU 也可用同构地址覆盖。
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_api_key: SecretStr = SecretStr("")
    mineru_model_version: Literal["pipeline", "vlm"] = "pipeline"
    # MinerU may need several minutes for a large document; the wait remains bounded.
    parser_timeout_seconds: float = Field(default=600.0, gt=0, le=600)

    # 分块与上下文边界
    # 本地接受上限（1 GiB，可调）：这是**本地**允许接收的原文件字节上限，
    # 与远端 provider 的硬限（MINERU_MAX_BYTES / MINERU_MAX_PAGES）是两个独立的数，
    # 单位都是「字节」，绝不再用 MiB/MB 混写。
    max_document_bytes: int = Field(default=1_073_741_824, ge=1024, le=8_589_934_592)
    # 远端 MinerU v4 单份硬限（官方文档：≤200,000,000 字节 **且** ≤200 页，两者都要满足）。
    # 超过它的非分片文件在本地就明确失败，不把必然被拒的文件送给 provider。
    mineru_max_bytes: int = Field(default=200_000_000, ge=1024, le=200_000_000)
    mineru_max_pages: int = Field(default=200, ge=1, le=2000)
    # 上传前的受控存储余量门控：低于该值时明确拒绝上传（409），而不是写到一半磁盘满。
    min_storage_free_bytes: int = Field(default=268_435_456, ge=0)
    # 解析文本的**显式预算**（不是静默截断）：超过即 Job 失败 extracted_text_too_large。
    # 上限 4,000,000 字符：与 document_contents.extracted_text 的 MEDIUMTEXT（16,777,215 字节）
    # 对齐——UTF-8 最坏 4 字节/字符时正好 16,000,000 字节，绝不会溢出该列。
    max_extracted_chars: int = Field(default=4_000_000, ge=10_000, le=4_000_000)
    max_upload_files: int = Field(default=1, ge=1, le=10)
    # 原文件受控存储根目录：容器内位于持久卷挂载点 /app/runtime 之下，
    # 宿主直跑时相对工作目录；API 只通过该目录的受控 key 访问，不接受客户端路径。
    upload_root: Path = Path("runtime/uploads")
    # 待调度 pending Job：等待超过该秒数仍未领取时由维护任务补调度（有界）。
    ingestion_pending_schedule_seconds: int = Field(default=15, ge=5, le=600)
    ingestion_schedule_batch: int = Field(default=10, ge=1, le=50)
    chunk_size_chars: int = Field(default=800, ge=100, le=4000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=1000)
    parent_chunk_chars: int = Field(default=2400, ge=200, le=20000)
    # 批处理边界：向量写入按批提交，避免大文档产生单次无界的 Milvus 请求。
    vector_upsert_batch_size: int = Field(default=256, ge=1, le=2000)
    max_retrieval_top_k: int = Field(default=20, ge=1, le=50)
    default_retrieval_top_k: int = Field(default=5, ge=1, le=50)
    max_context_chars: int = Field(default=6000, ge=500, le=50000)
    max_citations: int = Field(default=20, ge=1, le=100)

    # 结构化数据策略
    data_source_id: str = "demo_sales"
    schema_version: str = "demo-1"
    allowed_tables: str = "sales_orders,procurement_orders,products"
    masked_columns: str = "customer_contact,contact_phone,contact_email"
    sql_max_length: int = Field(default=4000, ge=64, le=20000)
    sql_max_rows: int = Field(default=100, ge=1, le=1000)
    sql_default_rows: int = Field(default=100, ge=1, le=1000)
    sql_max_joins: int = Field(default=3, ge=0, le=10)
    sql_max_subqueries: int = Field(default=2, ge=0, le=10)
    sql_max_unions: int = Field(default=2, ge=0, le=10)
    sql_max_select_expressions: int = Field(default=40, ge=1, le=200)
    sql_max_complexity_score: int = Field(default=60, ge=1, le=1000)
    sql_query_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    sql_max_result_bytes: int = Field(default=1_048_576, ge=1024, le=20_971_520)
    preview_default_rows: int = Field(default=50, ge=1, le=200)
    preview_max_rows: int = Field(default=200, ge=1, le=500)
    query_result_retention_seconds: int = Field(default=86400, ge=60, le=604800)
    query_result_max_rows: int = Field(default=1000, ge=1, le=10000)

    # 认证
    auth_session_ttl_seconds: int = Field(default=43200, ge=300, le=604800)
    auth_bootstrap_username: str = "demo"
    auth_bootstrap_password: SecretStr = SecretStr("")
    auth_bootstrap_display_name: str = "Demo User"

    # Approval / HITL
    approval_expiry_seconds: int = Field(default=1800, ge=60, le=86400)
    export_max_rows: int = Field(default=5000, ge=1, le=50000)
    # 后继 Run 的陈旧判定：带审批绑定、长时间未推进（updated_at 未变）的 running Run
    # 视为执行器已崩溃，可被维护任务转回可调度状态；默认 15 分钟，须远大于正常执行时长。
    run_stale_running_seconds: int = Field(default=900, ge=60, le=86400)
    # 同一 successor 的最大执行尝试次数（以 agent_start 事件计，含首次执行与每次恢复执行）：
    # 超过则终止为明确安全失败，避免崩溃恢复进入无限重试。
    run_max_recovery_attempts: int = Field(default=2, ge=1, le=10)
    # 执行期续租周期（独立心跳线程，ADR-035）：执行者按该周期刷新 Run 的
    # heartbeat_at/updated_at，使陈旧判定基于活跃证据而不是领取时刻，且不依赖
    # Graph 快照或模型输出（单步长阻塞也覆盖）。必须显著小于
    # RUN_STALE_RUNNING_SECONDS，否则合法长任务会被误判为陈旧并触发第二个执行者。
    run_heartbeat_seconds: float = Field(default=30.0, gt=0, le=300)

    # Ingestion 后台 worker
    ingestion_stale_running_seconds: int = Field(default=1800, ge=60, le=86400)
    ingestion_orphaned_pending_seconds: int = Field(default=300, ge=30, le=86400)
    ingestion_worker_claim_seconds: int = Field(default=120, ge=10, le=3600)
    # 摄取执行期续租周期（独立心跳线程，ADR-035）：解析/嵌入等单步可能长时间阻塞，
    # 因此续租必须由后台线程按周期推进，而不是依赖步骤边界。必须显著小于
    # INGESTION_STALE_RUNNING_SECONDS，否则合法长任务会被误判为陈旧。
    ingestion_heartbeat_seconds: float = Field(default=30.0, gt=0, le=300)

    # SSE
    sse_replay_retention_events: int = Field(default=1000, ge=10, le=10000)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=60)

    # API 边界
    conversations_default_limit: int = Field(default=20, ge=1, le=100)
    conversations_max_limit: int = Field(default=100, ge=1, le=100)
    messages_default_limit: int = Field(default=50, ge=1, le=200)
    messages_max_limit: int = Field(default=200, ge=1, le=200)
    documents_default_limit: int = Field(default=20, ge=1, le=100)
    documents_max_limit: int = Field(default=100, ge=1, le=100)
    tables_default_limit: int = Field(default=50, ge=1, le=200)
    tables_max_limit: int = Field(default=200, ge=1, le=200)

    @property
    def allowed_table_names(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.allowed_tables.split(",") if name.strip())

    @property
    def masked_column_names(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.masked_columns.split(",") if name.strip())

    @property
    def readonly_database(self) -> str:
        """只读账号与应用账号位于同一 schema 下。"""

        return self.mysql_database
