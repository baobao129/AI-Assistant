from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    mysql_host: str = "mysql"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_database: str = "enterprise_agent"
    mysql_user: str = "agent_app"
    mysql_password: SecretStr = SecretStr("")
    milvus_uri: str = "http://milvus:19530"
    checkpoint_mode: Literal["memory", "sqlite"] = "memory"
    checkpoint_sqlite_path: Path = Path("runtime/checkpoints.sqlite")
    max_tool_iterations: int = Field(default=8, ge=1, le=8)
    max_repair_attempts: int = Field(default=3, ge=0, le=3)
    max_answer_revisions: int = Field(default=1, ge=0, le=1)
    llm_provider: Literal["remote", "local", "mock"] = "remote"
    llm_base_url: str = "https://api.openai.com/v1"
    local_llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    local_llm_streaming: bool = False
    local_llm_structured_output: bool = False
    local_llm_tool_calling: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
