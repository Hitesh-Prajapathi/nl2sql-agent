from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # ── LLM provider ──────────────────────────────────────────────────────────
    # "groq" = real Groq models; "fake" = offline scripted/canned LLM (tests, load tests)
    llm_provider: Literal["groq", "fake"] = Field(default="groq")
    # Artificial per-call latency (ms) for the fake provider (load testing)
    fake_llm_latency_ms: float = Field(default=0.0)

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

    # ── Profiling (Phase 1.3) ─────────────────────────────────────────────────
    profile_table_token_budget: int = Field(default=1500)
    profile_dir: str = Field(default="data/profiles")

    # ── Multi-database / BIRD (Phase 1.1-1.2) ─────────────────────────────────
    # DSN template per db_id (one read-only role + pinned search_path per schema)
    bird_database_url_template: str = Field(
        default="postgresql://bird_ro_{db_id}:bird_ro_pass@localhost:5432/bird_minidev"
    )
    bird_data_dir: str = Field(default="data/bird_minidev")
    registry_pool_max_size: int = Field(default=3)
    # Longer than the interactive default: BIRD gold queries can legitimately run for seconds.
    eval_statement_timeout_ms: int = Field(default=30000)

    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    agent_version: str = Field(default="1.0.0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
