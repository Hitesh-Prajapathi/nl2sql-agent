# NL-to-SQL Agent

A production-grade Natural Language to SQL agent built using **LangGraph**, **Groq (Llama 3.3 70B)**, and **PostgreSQL**. 

This agent safely translates user questions into SQL, executes them against a local database, and synthesizes the results—all while featuring a defense-in-depth safety model (AST validation) and deep observability via Langfuse.

## Features
- **LangGraph State Machine**: Isolated nodes for schema caching, planning, safety validation, execution, and synthesis.
- **Full AST Safety Validation**: Parses LLM output using `sqlglot` to enforce strictly read-only (`SELECT`) queries. Drops any destructive operations instantly.
- **Smart Schema Profiling**: Caches not just columns and types, but row counts and low-cardinality values to drastically reduce LLM hallucinations.
- **End-to-End Tracing**: Fully instrumented with Langfuse for real-time observability of token usage, latency, and AST validation verdicts.

---

## Prerequisites
You will need the following installed:
- [Docker](https://www.docker.com/) (for spinning up the local Postgres DB)
- [uv](https://github.com/astral-sh/uv) (for ultra-fast Python package management)
- Python 3.12+

---

## Setup Instructions

### 1. Clone the repository and navigate into it
```bash
git clone <your-repo-link>
cd nl2sql-agent
```

### 2. Set up the environment
Create a `.env` file in the root of the project with your API keys:
```bash
GROQ_API_KEY=your_groq_api_key_here
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_SECRET_KEY=your_langfuse_secret_key
LANGFUSE_HOST=https://us.cloud.langfuse.com
```

### 3. Install Dependencies
We use `uv` to manage dependencies. Run:
```bash
uv sync
```
This will automatically create a `.venv` and install everything needed.

### 4. Start the Database
The project includes a `docker-compose.yml` to spin up a local PostgreSQL instance seeded with test data (a SaaS CRM schema).
```bash
docker compose up -d
```
*Wait a few seconds for Postgres to initialize.*

---

## Running the Agent

You can run the interactive REPL (Read-Eval-Print Loop) to chat with the agent in your terminal:
```bash
.venv/bin/python src/nl2sql/agent/run.py
```

Try asking it questions like:
- *"How many customers are in each tier?"*
- *"Who are the top 3 customers by total spending?"*
- *(Adversarial)* *"Drop the customers table."*

---

## Running Benchmarks

### 1. Latency Benchmark
Tests the raw speed of the agent across 10 concurrent requests, isolating LLM time vs framework overhead.
```bash
.venv/bin/python benchmarks/run_latency.py
```

### 2. Golden Set / Accuracy Test
Runs the agent against a suite of 20 pre-defined test cases (including complex joins and aggregations) and evaluates if the generated SQL executes correctly.
```bash
.venv/bin/python benchmarks/golden_set.py
```

---

## Technical Architecture
Curious about how the graph is designed, how we handle safety, or how we plan to scale to 200+ tables? 
Read the full technical teardown here: [WRITEUP.md](./WRITEUP.md)
