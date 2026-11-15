.PHONY: redteam-nl redteam-nl-setup eval-clarify bird-setup profile eval report prompt-size db-harden setup docker-up docker-down docker-logs dev cli ask test test-unit test-safety test-e2e bench redteam-sql lint fmt check logs

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

# NL-level red team (objective oracles; see redteam/nl/README.md). Uses its own database
# nl2sql_redteam. A real model needs RT_ARGS=--confirm-real-llm (the runner refuses otherwise).
LABEL ?= dev
RT_ARGS ?=
redteam-nl-setup:
	PYTHONPATH=$(PWD)/src uv run python redteam/nl/setup_db.py

redteam-nl:
	PYTHONPATH=$(PWD)/src uv run python redteam/nl/run_nl_suite.py --label $(LABEL) $(RT_ARGS)

# Clarification eval: 50 ambiguous + 50 clear questions (see evalsets/clarification/README.md).
# A real model needs CLAR_ARGS=--confirm-real-llm.
CLAR_ARGS ?=
eval-clarify:
	PYTHONPATH=$(PWD)/src uv run python evalsets/clarification/run_clarification_eval.py --label $(LABEL) $(CLAR_ARGS)

# Apply db/hardening.sql (REVOKE EXECUTE on dangerous built-ins, catalog lockdown) as superuser.
db-harden:
	./scripts/apply_db_hardening.sh

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
# ── BIRD Mini-Dev evaluation (Phase 1) ───────────────────────────────────────
VARIANT ?= full
LIMIT ?=
IDS ?=
DB ?=

bird-setup:
	uv run python scripts/bird_setup.py

profile:
	uv run python -m nl2sql.eval.cli profile $(if $(DB),--db $(DB),--all)

eval:
	uv run python -m nl2sql.eval.cli run --variant $(VARIANT) $(if $(LIMIT),--limit $(LIMIT)) $(if $(IDS),--ids $(IDS)) $(EVAL_ARGS)

report:
	uv run python scripts/report.py

# Offline (no LLM) prompt-size check over all 500 questions; fails if a *_small variant
# exceeds its cap.
prompt-size:
	uv run python scripts/check_prompt_size.py --variant $(VARIANT)

# ── Multi-worker (shared sessions + multiprocess metrics) ─────────────────────
# One-time: psql -U postgres -d nl2sql_assignment -f db/app_state.sql
.PHONY: dev-multi
dev-multi:
	rm -rf /tmp/nl2sql_prom && mkdir -p /tmp/nl2sql_prom
	SESSION_BACKEND=postgres PROMETHEUS_MULTIPROC_DIR=/tmp/nl2sql_prom \
		uv run uvicorn nl2sql.api.app:app --port 8000 --workers 4
