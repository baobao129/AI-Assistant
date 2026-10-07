from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration; secrets are never hard-coded here."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # Runtime
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    initialize_schema_on_startup: bool = True

    # MySQL application data
    mysql_host: str = "mysql"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "enterprise_agent"
    mysql_user: str = "agent_app"
    mysql_password: SecretStr = SecretStr("")
    mysql_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    mysql_pool_size: int = Field(default=5, ge=1, le=20)

    # MySQL read-only account used only by the SQL Agent query path. It always
    # targets the same server as the application account; only the credential differs.
    mysql_readonly_user: str = "agent_readonly"
    mysql_readonly_password: SecretStr = SecretStr("")
    mysql_readonly_pool_size: int = Field(default=3, ge=1, le=10)
    # Administrative credential used once to bootstrap the read-only account; never logged.
    mysql_root_password: SecretStr = SecretStr("")

    # Vector store
    milvus_uri: str = "http://milvus:19530"
    milvus_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Checkpoint
    checkpoint_mode: Literal["memory", "sqlite"] = "memory"
    checkpoint_sqlite_path: Path = Path("runtime/checkpoints.sqlite")

    # Bounded execution budgets
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
    # Structured Data Agent model selection: "auto" follows the LLM provider and uses
    # the deterministic agent for the mock/extractive development profiles.
    sql_agent_provider: Literal["auto", "llm", "scripted"] = "auto"

    # Embedding provider (SiliconFlow is the MVP remote provider)
    embedding_provider: Literal["siliconflow", "deterministic"] = "siliconflow"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dimension: int = Field(default=1024, ge=8, le=4096)
    embedding_api_key: SecretStr = SecretStr("")
    embedding_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    embedding_batch_size: int = Field(default=32, ge=1, le=128)

    # Parser
    parser_provider: Literal["lightweight", "mineru"] = "lightweight"
    mineru_base_url: str = "http://mineru:8000"
    parser_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    # Chunking and context bounds
    max_document_bytes: int = Field(default=5_242_880, ge=1024)
    max_upload_files: int = Field(default=1, ge=1, le=10)
    chunk_size_chars: int = Field(default=800, ge=100, le=4000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=1000)
    parent_chunk_chars: int = Field(default=2400, ge=200, le=20000)
    max_retrieval_top_k: int = Field(default=20, ge=1, le=50)
    default_retrieval_top_k: int = Field(default=5, ge=1, le=50)
    max_context_chars: int = Field(default=6000, ge=500, le=50000)
    max_citations: int = Field(default=20, ge=1, le=100)

    # Structured data policy
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

    # Authentication
    auth_session_ttl_seconds: int = Field(default=43200, ge=300, le=604800)
    auth_bootstrap_username: str = "demo"
    auth_bootstrap_password: SecretStr = SecretStr("")
    auth_bootstrap_display_name: str = "Demo User"

    # Approval / HITL
    approval_expiry_seconds: int = Field(default=1800, ge=60, le=86400)
    export_max_rows: int = Field(default=5000, ge=1, le=50000)

    # Ingestion workers
    ingestion_stale_running_seconds: int = Field(default=1800, ge=60, le=86400)
    ingestion_orphaned_pending_seconds: int = Field(default=300, ge=30, le=86400)
    ingestion_worker_claim_seconds: int = Field(default=120, ge=10, le=3600)

    # SSE
    sse_replay_retention_events: int = Field(default=1000, ge=10, le=10000)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=60)

    # API bounds
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
        """The read-only account lives in the same schema as the application account."""

        return self.mysql_database
