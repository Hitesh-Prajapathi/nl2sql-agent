.PHONY: setup dev test lint fmt check bench docker-up docker-down

# ── Setup ─────────────────────────────────────────────────────────────────────
setup:
	cp -n .env.example .env || true
	uv pip install -e ".[dev]"
	@echo "✅ Setup complete. Edit .env with your credentials, then run: make docker-up"

# ── Copy schema files from candidate materials ────────────────────────────────
sync-db:
	cp ../nl2sql_candidate_materials/schema.sql db/schema.sql
	cp ../nl2sql_candidate_materials/seed_data.sql db/seed_data.sql
	@echo "✅ Copied schema.sql and seed_data.sql into db/"

# ── Docker ────────────────────────────────────────────────────────────────────
docker-up:
	docker compose up -d postgres
	@echo "⏳ Waiting for Postgres to be healthy..."
	@until docker compose exec postgres pg_isready -U postgres -d nl2sql_assignment 2>/dev/null; do sleep 1; done
	@echo "✅ Postgres is ready at localhost:5432"

docker-down:
	docker compose down -v

docker-logs:
	docker compose logs -f postgres

# ── Run ───────────────────────────────────────────────────────────────────────
dev:
	uvicorn nl2sql.api.app:app --reload --port 8000

cli:
	uv run nl2sql chat

ask:
	uv run nl2sql ask "$(Q)"

# ── Testing ───────────────────────────────────────────────────────────────────
test:
	pytest tests/ -v --tb=short

test-safety:
	pytest tests/test_validator.py -v

test-e2e:
	pytest tests/test_graph_e2e.py -v --tb=short

# ── Benchmark ─────────────────────────────────────────────────────────────────
bench:
	uv run python benchmarks/run_benchmark.py

# ── Code Quality ──────────────────────────────────────────────────────────────
lint:
	ruff check src/ tests/

fmt:
	ruff format src/ tests/

check: lint
	mypy src/

# ── Convenience ───────────────────────────────────────────────────────────────
logs:
	tail -f nl2sql.log 2>/dev/null || echo "No log file found."
