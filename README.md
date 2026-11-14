# NL-to-SQL Agent

A LangGraph agent that answers natural-language questions about an e-commerce PostgreSQL database. It plans a SQL query, validates it with `sqlglot` (Postgres dialect) before execution, runs it, and synthesizes an answer. It uses Groq models and optional Langfuse tracing.

## Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Docker (for the local Postgres container)
- A Groq API key

## Setup

```bash
cp .env.example .env        # then set GROQ_API_KEY
make setup                  # uv sync --extra dev
make docker-up              # Postgres on localhost:5432, seeded from db/
```

`.env.example` documents every setting. Its `DATABASE_URL` points at the Postgres container on `localhost`.

## Running

Interactive CLI:

```bash
make cli                                  # multi-turn chat
make ask Q="How many customers are in each tier?"
uv run nl2sql ask "Top 3 customers by total spending" --json
```

HTTP API (FastAPI, with reload):

```bash
make dev                                  # UI http://localhost:8000 · API docs http://localhost:8000/docs
curl -s localhost:8000/health
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "How many orders were placed last month?"}'
```

Full stack in Docker (no reload, healthcheck on `/health`):

```bash
docker compose up --build app
```

For live reload with the source bind-mounted, use `docker compose --profile dev up --build app-dev`. Do not run it alongside `app`, since both publish port 8000.

## Tests

```bash
make test          # all tests
make test-unit     # excludes tests marked integration
make test-safety   # SQL safety and injection-guard tests
make test-e2e      # graph end-to-end tests
```

## Benchmarks

- `make bench` runs `benchmarks/run_latency.py` against a running API (`make dev` first). Results are written to `benchmarks/results/*.json`.
- `make redteam-sql` runs the SQL red-team suite in `redteam/`.
- `make eval VARIANT=full_small` runs the BIRD Mini-Dev evaluation (variants: `full`, `naive`, `full_small`, `naive_small`). The `*_small` variants cap prompt + output tokens at 6,500 per request for the Groq free tier (8,000 tokens/minute and 1,000 requests/day per model). The runner paces itself, stops cleanly when the daily budget is used up, and resumes from `results/bird_minidev_<variant>.jsonl` when the same command is rerun. `make prompt-size VARIANT=full_small` checks the cap offline for all 500 questions.
- `benchmarks/golden_set.py` is a data module of test cases. It is not a runnable script.

## Results

Benchmarks in progress; see docs/REFINEMENT_EXECUTION_PLAN.md. The tables below are generated
by `make report` from complete (500-question) runs only.

<!-- RESULTS:START -->
_No complete BIRD Mini-Dev run has been recorded yet. Run `make eval`._
<!-- RESULTS:END -->

## Development

```bash
make lint   # ruff
make fmt    # ruff format
make check  # mypy src/
```

## Architecture
See [WRITEUP.md](./WRITEUP.md) for the design notes.
