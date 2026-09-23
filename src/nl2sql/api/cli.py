from __future__ import annotations

import asyncio
import json
from typing import Any
import uuid

import structlog
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from nl2sql.db.connection import close_pool, get_pool
from nl2sql.agent.graph import build_graph, run_query
from nl2sql.telemetry.setup import flush_langfuse, get_langfuse_handler

app = typer.Typer(name="nl2sql", help="NL-to-SQL LangGraph Agent CLI")
console = Console()
log = structlog.get_logger()


@app.command()
def ask(
    question: str = typer.Argument(..., help="Natural language question about the database"),
    thread_id: str = typer.Option(None, "--thread", "-t", help="Thread ID for multi-turn conversation"),
    show_sql: bool = typer.Option(True, "--sql/--no-sql", help="Show generated SQL"),
    show_timings: bool = typer.Option(False, "--timings", help="Show per-node latency breakdown"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON response"),
) -> None:
    """Ask a single question and print the answer."""
    asyncio.run(_ask(question, thread_id, show_sql, show_timings, json_output))


@app.command()
def chat(
    thread_id: str = typer.Option(None, "--thread", "-t", help="Thread ID (auto-generated if not set)"),
) -> None:
    """Start an interactive multi-turn chat session."""
    asyncio.run(_chat(thread_id))


async def _ask(
    question: str,
    thread_id: str | None,
    show_sql: bool,
    show_timings: bool,
    json_output: bool,
) -> None:
    pool = await get_pool()
    graph = build_graph(pool)
    thread = thread_id or str(uuid.uuid4())
    handler = get_langfuse_handler(session_id=thread)
    config: dict[str, Any] = {"callbacks": [handler]} if handler else {}

    try:
        state = await run_query(graph, question, thread_id=thread)
        _print_response(state, show_sql, show_timings, json_output)
    finally:
        flush_langfuse()
        await close_pool()


async def _chat(thread_id: str | None) -> None:
    pool = await get_pool()
    graph = build_graph(pool)
    thread = thread_id or str(uuid.uuid4())
    handler = get_langfuse_handler(session_id=thread)

    console.print(Panel(
        f"[bold cyan]NL-to-SQL Agent[/bold cyan]\n"
        f"Thread: [dim]{thread}[/dim]\n"
        "Type [bold]exit[/bold] or [bold]quit[/bold] to end the session.",
        title="💬 Chat Mode",
        border_style="cyan",
    ))

    messages: list[Any] = []
    try:
        while True:
            try:
                question = typer.prompt("\n[You]")
            except (typer.Abort, EOFError):
                break

            if question.strip().lower() in ("exit", "quit", "q"):
                break

            state = await run_query(graph, question, thread_id=thread, messages=messages)
            messages = state.get("messages", [])
            _print_response(state, show_sql=True, show_timings=False, json_output=False)
    finally:
        flush_langfuse()
        await close_pool()
        console.print("\n[dim]Session ended.[/dim]")


def _print_response(state: dict[str, Any], show_sql: bool, show_timings: bool, json_output: bool) -> None:
    if json_output:
        output = {
            "status": state.get("status"),
            "answer": state.get("final_answer"),
            "sql": state.get("final_sql"),
            "assumptions": state.get("assumptions", []),
            "attempts": state.get("attempts", []),
            "timings": state.get("timings", {}),
            "trace_id": state.get("trace_id"),
        }
        console.print_json(json.dumps(output, default=str))
        return

    status = state.get("status", "unknown")
    status_icons = {
        "answered": "✅",
        "needs_clarification": "🤔",
        "refused": "⛔",
        "failed": "❌",
    }
    icon = status_icons.get(status, "❓")

    console.print(f"\n{icon} [bold]{status.upper()}[/bold]")
    console.print(Markdown(state.get("final_answer", "")))

    assumptions = state.get("assumptions", [])
    if assumptions:
        console.print("\n[dim]Assumptions:[/dim]")
        for a in assumptions:
            console.print(f"  [dim]• {a}[/dim]")

    if show_sql and state.get("final_sql"):
        console.print("\n[dim]SQL:[/dim]")
        console.print(Syntax(state["final_sql"], "sql", theme="monokai", line_numbers=False))

    attempts = state.get("attempts", [])
    if len(attempts) > 0:
        console.print(f"\n[dim]Attempts: {len(attempts)} retry(ies)[/dim]")

    if show_timings and state.get("timings"):
        console.print("\n[dim]Timings:[/dim]")
        for node, ms in state["timings"].items():
            console.print(f"  [dim]{node}: {ms}ms[/dim]")


if __name__ == "__main__":
    app()
