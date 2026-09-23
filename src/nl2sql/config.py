from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql://nl2sql_reader:reader_pass@localhost:5432/nl2sql_assignment"
    )
    database_admin_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/nl2sql_assignment"
    )

    # ── Groq ──────────────────────────────────────────────────────────────────
    groq_api_key: str = Field(default="")

    # ── Models ────────────────────────────────────────────────────────────────
    planner_model: str = Field(default="llama-3.3-70b-versatile")
    fast_model: str = Field(default="llama-3.1-8b-instant")
    fallback_model: str = Field(default="llama-4-scout-17b")

    # ── Langfuse ─────────────────────────────────────────────────────────────
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    langfuse_host: str = Field(default="https://cloud.langfuse.com")

    # ── Agent Behaviour ───────────────────────────────────────────────────────
    max_retry_attempts: int = Field(default=3)
    query_row_cap: int = Field(default=500)
    statement_timeout_ms: int = Field(default=15000)
    lock_timeout_ms: int = Field(default=5000)
    schema_cache_ttl_seconds: int = Field(default=300)

    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    agent_version: str = Field(default="1.0.0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
