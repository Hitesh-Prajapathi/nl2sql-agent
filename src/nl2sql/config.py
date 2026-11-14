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

    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    agent_version: str = Field(default="1.0.0")

    # ── Phase 5: API hardening ────────────────────────────────────────────────
    # Comma-separated "key_id:sha256hex" entries. Empty = auth disabled (local dev).
    api_key_hashes: str = Field(default="")
    api_key_header: str = Field(default="X-API-Key")
    cors_allow_origins: str = Field(default="http://localhost:8000,http://127.0.0.1:8000")
    max_question_chars: int = Field(default=2000)
    rate_limit_per_key_per_min: int = Field(default=60)
    rate_limit_per_ip_per_min: int = Field(default=120)
    rate_limit_enabled: bool = Field(default=True)

    # ── Sessions (in-memory, per process) ────────────────────────────────────
    session_ttl_seconds: int = Field(default=3600)
    session_max_turns: int = Field(default=20)
    session_max_bytes: int = Field(default=65536)
    session_max_sessions: int = Field(default=1000)

    # ── LLM resilience ────────────────────────────────────────────────────────
    llm_timeout_seconds: float = Field(default=30.0)
    llm_max_retries: int = Field(default=3)
    llm_backoff_base_seconds: float = Field(default=0.5)
    llm_backoff_max_seconds: float = Field(default=8.0)
    llm_max_concurrency: int = Field(default=8)
    llm_breaker_failure_threshold: int = Field(default=5)
    llm_breaker_reset_seconds: float = Field(default=30.0)

    # ── Cost controls (prices are USD per 1M tokens; unset = cost unknown) ───
    price_input_per_1m_usd: float | None = Field(default=None)
    price_output_per_1m_usd: float | None = Field(default=None)
    daily_spend_cap_usd: float | None = Field(default=None)
    llm_kill_switch: bool = Field(default=False)

    # ── Audit log (separate small pool, role audit_writer) ───────────────────
    audit_enabled: bool = Field(default=True)
    audit_database_url: str = Field(
        default="postgresql://audit_writer:writer_pass@localhost:5432/nl2sql_assignment"
    )
    audit_pool_max_size: int = Field(default=2)

    # ── Observability ─────────────────────────────────────────────────────────
    # OTLP/HTTP endpoint, e.g. http://localhost:4318. Empty = no exporter.
    otel_exporter_otlp_endpoint: str = Field(default="")
    otel_service_name: str = Field(default="nl2sql-agent")
    metrics_enabled: bool = Field(default=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
