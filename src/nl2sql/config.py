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
    # Groq free tier on this key: every model is limited to 1,000 requests/day and 8,000
    # tokens/minute (see the eval pacer in nl2sql.eval.pacer).
    planner_model: str = Field(default="openai/gpt-oss-120b")
    fast_model: str = Field(default="openai/gpt-oss-20b")
    fallback_model: str = Field(default="openai/gpt-oss-20b")
    # Reasoning effort for gpt-oss models (low | medium | high). Reasoning tokens count against
    # the output token cap, so "low" keeps the JSON answer from being truncated.
    llm_reasoning_effort: str = Field(default="low")
    # Output token caps per call. They are reserved against the per-minute token limit.
    planner_max_tokens: int = Field(default=2048)
    fast_max_tokens: int = Field(default=1024)

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
    llm_max_concurrency: int = Field(default=32)
    llm_breaker_failure_threshold: int = Field(default=5)
    llm_breaker_reset_seconds: float = Field(default=30.0)

    # ── Rate-limit pacing (Groq free tier: per model) ─────────────────────────
    # Client-side token bucket + daily request budget, applied when LLM_PROVIDER=groq.
    # Turn it off (LLM_PACING_ENABLED=false) on a paid tier.
    llm_pacing_enabled: bool = Field(default=True)
    llm_tpm_limit: int = Field(default=8000)
    llm_rpd_limit: int = Field(default=1000)

    # ── Kill switch ───────────────────────────────────────────────────────────
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

    # ── Shared session store (multi-worker) ───────────────────────────────────
    # memory (per process, default) | postgres | redis. Use postgres or redis with
    # more than one uvicorn worker, otherwise follow-ups can land on a worker that
    # has no history for the thread.
    session_backend: str = Field(default="memory")
    # Postgres backend: role app_state_writer over its own small pool (db/app_state.sql).
    session_database_url: str = Field(
        default="postgresql://app_state_writer:app_state_pass@localhost:5432/nl2sql_assignment"
    )
    session_pool_max_size: int = Field(default=3)
    # Redis backend (needs the `redis` extra: uv sync --extra redis).
    session_redis_url: str = Field(default="redis://localhost:6379/0")
    session_redis_prefix: str = Field(default="nl2sql:session:")


@lru_cache
def get_settings() -> Settings:
    return Settings()
