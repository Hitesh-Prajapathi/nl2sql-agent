from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nl2sql.agent.graph import build_graph, run_query
from nl2sql.config import get_settings
from nl2sql.db.connection import close_pool, get_pool
from nl2sql.models import AgentResponse
from nl2sql.telemetry.setup import flush_langfuse, get_langfuse_handler

log = structlog.get_logger()
settings = get_settings()

_graph = None
# In-memory session store: thread_id → list of conversation turns
_sessions: dict[str, list[dict]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    pool = await get_pool()
    _graph = build_graph(pool)
    log.info("app_started", version=settings.agent_version)
    yield
    flush_langfuse()
    await close_pool()
    log.info("app_stopped")


app = FastAPI(
    title="NL-to-SQL Agent",
    version=settings.agent_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    question: str
    thread_id: str | None = None


class QueryResponse(BaseModel):
    status: str
    answer: str | None
    sql: str | None
    columns: list[str]           # raw column names from DB result
    rows: list[dict]             # raw result rows for frontend table rendering
    assumptions: list[str]
    attempts: int
    trace_id: str | None
    timings: dict[str, float]


@app.get("/health")
async def health():
    return {"status": "ok", "version": settings.agent_version}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    thread_id = req.thread_id or str(uuid.uuid4())
    history = _sessions.get(thread_id, [])
    lf_handler = get_langfuse_handler(session_id=thread_id)

    try:
        state = await run_query(
            _graph, req.question, thread_id=thread_id,
            conversation_history=history,
            callbacks=[lf_handler] if lf_handler else None,
        )
    except Exception as exc:
        log.error("query_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    # Persist this turn for future follow-ups
    _sessions[thread_id] = history + [{
        "question": req.question,
        "decision": state.get("decision"),
        "clarification": state.get("clarification_question", ""),
        "sql": state.get("final_sql") or "",
        "answer": state.get("final_answer") or "",
    }]

    return QueryResponse(
        status=state.get("status", "failed"),
        answer=state.get("final_answer"),
        sql=state.get("final_sql"),
        columns=state.get("exec_columns", []),
        rows=state.get("exec_rows", []),
        assumptions=state.get("assumptions", []),
        attempts=len(state.get("attempts", [])),
        trace_id=state.get("trace_id"),
        timings=state.get("timings", {}),
    )


# ── Static frontend — must be mounted LAST so API routes take priority ────────
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
