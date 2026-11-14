from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.graph.state import CompiledStateGraph
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from nl2sql.agent.graph import build_graph, run_query
from nl2sql.agent.state import AgentState
from nl2sql.api.errors import ApiError, install_error_handlers, request_id_ctx
from nl2sql.api.security import (
    InMemoryTokenBucket,
    RateLimiter,
    auth_enabled,
    authenticate,
)
from nl2sql.api.sessions import InMemorySessionStore, SessionStore
from nl2sql.api.spend import SpendGuard
from nl2sql.audit.writer import AuditRecord, AuditWriter, hash_question
from nl2sql.config import get_settings
from nl2sql.db.connection import close_pool, get_pool
from nl2sql.llm.providers import LLMUnavailableError
from nl2sql.telemetry import metrics
from nl2sql.telemetry.setup import flush_langfuse, get_langfuse_handler
from nl2sql.telemetry.tracing import (
    RequestObserver,
    current_trace_id,
    get_tracer,
    setup_tracing,
    shutdown_tracing,
)

log = structlog.get_logger()
settings = get_settings()

Graph = CompiledStateGraph[AgentState, Any, AgentState, AgentState]


class QueryRequest(BaseModel):
    question: str
    thread_id: str | None = None


class QueryResponse(BaseModel):
    status: str
    answer: str | None
    sql: str | None
    columns: list[str]  # raw column names from DB result
    rows: list[dict[str, Any]]  # raw result rows for frontend table rendering
    assumptions: list[str]
    attempts: int
    trace_id: str | None
    timings: dict[str, float]
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # None when token usage or prices are unavailable


class RequestIdMiddleware:
    """Pure-ASGI middleware: accepts/generates X-Request-ID and echoes it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = ""
        for k, v in scope["headers"]:
            if k == b"x-request-id":
                rid = v.decode("latin-1")[:64]
        if not rid or not rid.replace("-", "").replace("_", "").isalnum():
            rid = uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        structlog.contextvars.bind_contextvars(request_id=rid)

        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_ctx.reset(token)


def _blocked_layer(state: dict[str, Any]) -> str | None:
    if state.get("is_blocked"):
        return "ast_validator"
    if state.get("is_safety_exec_block"):
        return "database"
    return None


class _StreamProxy:
    """Drop-in for the compiled graph that runs LangGraph's streaming API and
    forwards node updates to ``emit`` while still returning the final state."""

    def __init__(self, graph: Graph, emit: Callable[[dict[str, Any]], None]) -> None:
        self._graph = graph
        self._emit = emit

    async def ainvoke(self, state: Any, config: Any = None) -> dict[str, Any]:
        final: dict[str, Any] = {}
        async for mode, chunk in self._graph.astream(
            state, config=config, stream_mode=["updates", "values"]
        ):
            if mode == "updates":
                for node in chunk:
                    self._emit({"event": "node", "node": node})
            else:
                final = cast("dict[str, Any]", chunk)
        return final


def create_app(
    graph_factory: Callable[[], Awaitable[Graph]] | None = None,
    ready_check: Callable[[], Awaitable[None]] | None = None,
    rate_limiter: RateLimiter | None = None,
    sessions: SessionStore | None = None,
    audit: AuditWriter | None = None,
) -> FastAPI:
    s = get_settings()
    setup_tracing()

    async def _default_graph() -> Graph:
        return build_graph(await get_pool())

    async def _default_ready() -> None:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")

    audit_writer = audit or AuditWriter()
    limiter: RateLimiter = rate_limiter or InMemoryTokenBucket()
    store: SessionStore = sessions or InMemorySessionStore(
        s.session_ttl_seconds, s.session_max_turns, s.session_max_bytes, s.session_max_sessions
    )
    spend = SpendGuard(sync=audit_writer.spent_today_usd)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.graph = await (graph_factory or _default_graph)()
        log.info("app_started", version=s.agent_version)
        yield
        flush_langfuse()
        await audit_writer.close()
        if graph_factory is None:
            await close_pool()
        shutdown_tracing()
        log.info("app_stopped")

    app = FastAPI(title="NL-to-SQL Agent", version=s.agent_version, lifespan=lifespan)
    app.state.spend = spend
    app.state.audit = audit_writer
    app.state.sessions = store
    install_error_handlers(app)

    origins = [o.strip() for o in s.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=[s.api_key_header, "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)

    # ── guards ────────────────────────────────────────────────────────────────
    async def guard(request: Request) -> str | None:
        """Rate limit by IP, authenticate, rate limit by key. Returns the key ID."""
        cfg = get_settings()
        ip = request.client.host if request.client else "unknown"
        if cfg.rate_limit_enabled:
            ok, wait = await limiter.allow(f"ip:{ip}", cfg.rate_limit_per_ip_per_min)
            if not ok:
                metrics.RATE_LIMITED.labels("ip").inc()
                raise ApiError(
                    429, "rate_limited", "Too many requests.", {"Retry-After": str(int(wait) + 1)}
                )
        key_id: str | None = None
        if auth_enabled():
            key_id = authenticate(request.headers.get(cfg.api_key_header))
            if key_id is None:
                raise ApiError(401, "unauthorized", "Missing or invalid API key.")
            if cfg.rate_limit_enabled:
                ok, wait = await limiter.allow(f"key:{key_id}", cfg.rate_limit_per_key_per_min)
                if not ok:
                    metrics.RATE_LIMITED.labels("key").inc()
                    raise ApiError(
                        429,
                        "rate_limited",
                        "Too many requests.",
                        {"Retry-After": str(int(wait) + 1)},
                    )
        return key_id

    # ── core query pipeline ───────────────────────────────────────────────────
    async def process(
        req: QueryRequest,
        key_id: str | None,
        route: str,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> QueryResponse:
        cfg = get_settings()
        question = req.question.strip()
        if not question:
            raise ApiError(400, "empty_question", "Question cannot be empty.")
        if len(question) > cfg.max_question_chars:
            raise ApiError(
                413,
                "question_too_long",
                f"Question exceeds {cfg.max_question_chars} characters.",
            )
        graph = cast("Graph | None", getattr(app.state, "graph", None))
        if graph is None:
            raise ApiError(503, "not_ready", "Agent not initialised.")
        blocked = await spend.reason_blocked()
        if blocked:
            metrics.REQUESTS.labels(route, blocked).inc()
            raise ApiError(503, blocked, "LLM usage is currently disabled.")

        thread_id = req.thread_id or str(uuid.uuid4())
        history = await store.get(thread_id)
        observer = RequestObserver()
        callbacks: list[Any] = [observer]
        lf_handler = get_langfuse_handler(session_id=thread_id)
        if lf_handler:
            callbacks.append(lf_handler)

        runner: Any = _StreamProxy(graph, emit) if emit else graph
        t0 = time.perf_counter()
        rid = request_id_ctx.get()
        state: dict[str, Any] = {}
        outcome = "error"
        trace_id: str | None = None
        try:
            with get_tracer().start_as_current_span("request") as span:
                span.set_attribute("request.id", rid)
                span.set_attribute("route", route)
                trace_id = current_trace_id()
                state = await run_query(
                    runner,
                    question,
                    thread_id=thread_id,
                    conversation_history=history,
                    callbacks=callbacks,
                )
                outcome = state.get("status", "failed")
                span.set_attribute("status", outcome)
        except LLMUnavailableError as exc:
            raise ApiError(503, "llm_unavailable", "The language model is unavailable.") from exc
        except Exception as exc:
            log.error("query_error", error_type=type(exc).__name__)
            raise ApiError(500, "internal_error", "Internal server error.") from exc
        finally:
            latency = time.perf_counter() - t0
            metrics.REQUEST_LATENCY.labels(route).observe(latency)
            metrics.REQUESTS.labels(route, outcome).inc()
            layer = _blocked_layer(state)
            if layer:
                metrics.BLOCKS.labels(layer).inc()
            cost = observer.cost_usd
            if cost:
                metrics.COST.inc(cost)
                spend.record(cost)
            await audit_writer.write(
                AuditRecord(
                    request_id=rid,
                    key_id=key_id,
                    question_hash=hash_question(question),
                    sql_text=state.get("final_sql") or state.get("generated_sql") or None,
                    status=outcome,
                    blocked_layer=layer,
                    row_count=state.get("exec_row_count") if state else None,
                    latency_ms=round(latency * 1000, 2),
                    cost_usd=cost,
                    trace_id=trace_id or state.get("trace_id") or None,
                )
            )

        await store.append(
            thread_id,
            {
                "question": question,
                "decision": state.get("decision"),
                "clarification": state.get("clarification_question", ""),
                "sql": state.get("final_sql") or "",
                "answer": state.get("final_answer") or "",
            },
        )
        return QueryResponse(
            status=state.get("status", "failed"),
            answer=state.get("final_answer"),
            sql=state.get("final_sql"),
            columns=state.get("exec_columns", []),
            rows=state.get("exec_rows", []),
            assumptions=state.get("assumptions", []),
            attempts=len(state.get("attempts", [])),
            trace_id=state.get("trace_id") or trace_id,
            timings=state.get("timings", {}),
            request_id=rid,
            input_tokens=observer.input_tokens if observer.has_usage else None,
            output_tokens=observer.output_tokens if observer.has_usage else None,
            cost_usd=cost,
        )

    # ── routes ────────────────────────────────────────────────────────────────
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": s.agent_version}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        try:
            await asyncio.wait_for((ready_check or _default_ready)(), timeout=3)
            if getattr(app.state, "graph", None) is None:
                raise RuntimeError("graph not built")
        except Exception as exc:
            log.warning("not_ready", error_type=type(exc).__name__)
            return JSONResponse({"status": "unavailable", "checks": {"db": "fail"}}, 503)
        return JSONResponse({"status": "ready", "checks": {"db": "ok"}})

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        if not get_settings().metrics_enabled:
            raise ApiError(404, "not_found", "Not found.")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/query", response_model=QueryResponse)
    @app.post("/query", response_model=QueryResponse)
    async def query(req: QueryRequest, key_id: str | None = Depends(guard)) -> QueryResponse:
        return await process(req, key_id, "query")

    @app.post("/v1/query/stream")
    async def query_stream(req: QueryRequest, key_id: str | None = Depends(guard)) -> Response:
        # Validate eagerly so errors are normal JSON envelopes, not SSE frames.
        if not req.question.strip():
            raise ApiError(400, "empty_question", "Question cannot be empty.")
        if len(req.question.strip()) > get_settings().max_question_chars:
            raise ApiError(413, "question_too_long", "Question is too long.")
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        rid = request_id_ctx.get()

        async def worker() -> None:
            request_id_ctx.set(rid)
            try:
                resp = await process(req, key_id, "stream", emit=queue.put_nowait)
                queue.put_nowait({"event": "result", "data": resp.model_dump()})
            except ApiError as exc:
                queue.put_nowait(
                    {"event": "error", "data": {"code": exc.code, "message": exc.message}}
                )
            finally:
                queue.put_nowait(None)

        async def gen() -> AsyncIterator[str]:
            task = asyncio.create_task(worker())
            try:
                while (item := await queue.get()) is not None:
                    data = item.get("data", {k: v for k, v in item.items() if k != "event"})
                    yield f"event: {item['event']}\ndata: {json.dumps(data, default=str)}\n\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    # ── Static frontend — must be mounted LAST so API routes take priority ────
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    return app


app = create_app()
