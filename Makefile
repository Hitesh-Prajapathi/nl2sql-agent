.PHONY: setup docker-up docker-down docker-logs dev cli ask test test-unit test-safety test-e2e bench redteam-sql lint fmt check logs

# ── Setup ─────────────────────────────────────────────────────────────────────
setup:
	cp -n .env.example .env || true
	uv sync --extra dev
	@echo "Setup complete. Edit .env with your credentials, then run: make docker-up"

# ── Docker (Postgres only; the full stack is `docker compose up app`) ────────
docker-up:
	docker compose up -d postgres
	@echo "Waiting for Postgres to be healthy..."
	@for i in $$(seq 1 60); do \
		docker compose exec -T postgres pg_isready -U postgres -d nl2sql_assignment >/dev/null 2>&1 && break; \
		sleep 1; \
	done
	@docker compose exec -T postgres pg_isready -U postgres -d nl2sql_assignment
	@echo "Postgres is ready at localhost:5432"

docker-down:
	docker compose down -v

docker-logs:
	docker compose logs -f postgres

# ── Run ───────────────────────────────────────────────────────────────────────
dev:
	uv run uvicorn nl2sql.api.app:app --reload --port 8000

cli:
	uv run nl2sql chat

ask:
	uv run nl2sql ask "$(Q)"

# ── Testing ───────────────────────────────────────────────────────────────────
test:
	uv run pytest -q

test-unit:
	uv run pytest -q -m "not integration"

test-safety:
	uv run pytest -q tests/test_safety.py tests/test_redteam_sql.py tests/test_injection_guard.py

test-e2e:
	uv run pytest -q tests/test_graph_e2e.py

# ── Benchmark and red-team ────────────────────────────────────────────────────
# bench needs the API running (`make dev`) and POSTs to it.
bench:
	uv run python benchmarks/run_latency.py

redteam-sql:
	uv run python redteam/run_sql_suite.py

# ── Code Quality ──────────────────────────────────────────────────────────────
lint:
	uv run ruff check src/ tests/

fmt:
	uv run ruff format src/ tests/

check: lint
	uv run mypy src/

# ── Convenience ───────────────────────────────────────────────────────────────
logs:
	tail -f nl2sql.log 2>/dev/null || echo "No log file found."

# ── Load test (mock LLM; needs Postgres, starts its own uvicorn on :8765) ────
loadtest:
	# caffeinate (macOS) stops host sleep from stalling the measurement
	PYTHONPATH=$(PWD)/src:$(PWD) $(shell command -v caffeinate >/dev/null && echo "caffeinate -dims") uv run --extra loadtest python loadtest/run.py
