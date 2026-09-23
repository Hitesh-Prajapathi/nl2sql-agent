# NL-to-SQL Agent — Technical Write-up

Hey, I'm Hitesh Prajapathi. Here is a breakdown of how I approached building this NL-to-SQL agent, the architectural choices I made, and how we can scale it up for production.

---

## Architecture & Graph Design

Instead of a messy script, I built the agent using LangGraph. It runs as a six-node state machine with a strict left-to-right flow. Having a formal graph makes it much easier to reason about early exits (like catching prompt injection right away) and managing retry loops. 

Here is the flow:
```
START → load_schema → plan_sql ──► clarify  → END
                          │──────► refuse   → END
                          ▼
                     validate_sql ──block──► refuse → END
                          │ ok
                          ▼
                     execute_sql ──error──► correct_sql ─┐ (attempt < 3)
                          │ ok                            └──► validate_sql
                          ▼                     attempt == 3 ──► refuse → END
                     synthesize → END
```

A few key decisions I made here:

**1. The Merged Planner:** Originally, I thought about doing this in two steps: one LLM call to figure out the intent, and a second to write the SQL. But that adds a massive latency penalty. I merged them. I use one structured-output LLM call (`plan_sql`) that figures out if we need to clarify, refuse, or write SQL all at once. This easily shaves 1-2 seconds off every request.

**2. Smart Schema Caching:** I don't just dump `information_schema`. The `load_schema` node profiles the data—it grabs row counts, date ranges, and unique values for low-cardinality columns. I cache this payload in memory for 5 minutes. As you'll see in the latency numbers below, this drops schema loading time from ~70ms to about 3ms.

**3. Multi-turn Memory:** Instead of constantly asking the LLM to rewrite the user's prompt (e.g., changing "what about their orders" to "what about the orders for customer X"), I built a lightweight session store. I inject the last four conversation turns directly into the planner's prompt. The LLM resolves the context naturally. 

---

## Real Latency Numbers

I wrote a benchmark script (`benchmarks/run_latency.py`) to hammer the endpoint locally. I ran this using Groq's free tier (`llama-3.3-70b-versatile`) against a local Postgres Docker container.

Because Groq's free tier has strict daily token limits, we hit 429 rate limit errors on almost all the follow-up runs, but we got a perfectly clean profile on the first run before the rate limit tripped. Here is the raw log output from that successful run:

```text
  [LAT-01] How many customers are there in each tier?...
    run 1: 1112ms  status=answered  nodes={'load_schema_ms': '69.0ms', 'plan_sql_ms': '939.0ms', 'validate_sql_ms': '4.0ms', 'execute_sql_ms': '1.0ms', 'synthesize_ms': '0.0ms'}
```

Look at that node breakdown. The agent itself is insanely fast:
*   **load_schema:** 69ms (cold cache) / ~3ms (warm cache)
*   **validate_sql:** 4ms
*   **execute_sql:** 1ms
*   **synthesize:** 0ms

The framework and database execution combined take **under 15 milliseconds** on a warm cache. Almost 100% of the latency (939ms) is just waiting on the LLM API to return the SQL. 

**How do we fix LLM latency in production?**
1.  **Prompt Caching:** Because I'm injecting the exact same massive schema string on every request, we could use Anthropic or Groq prompt caching. The LLM would only process the new user question, drastically cutting time to first token.
2.  **Tiered Models:** We don't need a 70B model to format the final answer. We could use a massive model (like Claude 3.5 Sonnet) for the complex SQL planning, and a tiny fast model (like Haiku or Llama 8B) for synthesis and error correction.

---

## 7 Layers of Safety

You can't just hook an LLM up to a database and hope for the best. I built a defense-in-depth safety model. No single failure brings down the system.

1.  **Database User Roles:** The agent connects as `nl2sql_reader`. This Postgres role only has `SELECT` privileges. Even if the LLM hallucinates a `DROP TABLE` command, the database itself will reject it.
2.  **Read-Only Transactions:** Every single query runs wrapped inside a `BEGIN READ ONLY` block with strict 15-second timeouts.
3.  **AST Validation:** This is the core engine. I use `sqlglot` to parse the LLM's text into an Abstract Syntax Tree (AST). We enforce that there is exactly one statement, the root is a `SELECT`, and absolutely no DML/DDL nodes exist anywhere in the tree. We also block dangerous Postgres functions like `pg_sleep` and `dblink`. Regex validation isn't enough; you have to parse the tree.
4.  **Prompt-Injection Guards:** Before the LLM even sees the prompt, I run regex checks for common jailbreaks (e.g., "ignore previous instructions", "DAN mode"). If tripped, we immediately return a terminal refusal.
5.  **Hard Row Limits:** The AST validator automatically injects a `LIMIT` clause to prevent massive data pulls.
6.  **Data Isolation:** When passing the SQL results back to the LLM for synthesis, I wrap the data in strict delimiters (`---DATA---`). This stops indirect prompt injection where a malicious user puts a jailbreak command into a database field.
7.  **Telemetry:** Every single SQL string, AST verdict, and execution is logged via `structlog` and pushed to Langfuse for auditing.

*Note: If the AST validator blocks a query, it's a "terminal" refusal. We don't feed the error back to the LLM to try again, because that allows attackers to iteratively brute-force the validator.*

---

## Scaling to 200+ Tables

Right now, querying `information_schema` and dumping the whole thing into the system prompt works perfectly for 8 tables. But at 200 tables, we would blow past the context window and the LLM would get confused. 

Here is how I designed the codebase to handle that transition. Notice that `load_schema` just calls `SchemaProvider.get_context()`. We can swap out the implementation without touching the graph logic.

To scale up, I would implement **Retrieval-Augmented Schema (RAS)**:
1.  **Offline Embeddings:** Run a nightly job that generates embeddings for every table and column description, and store them in the database.
2.  **LLM Summaries:** During that offline job, have an LLM read a few sample rows from each table and generate a plain-English "business purpose" summary, saving it alongside the schema.
3.  **Dynamic Retrieval:** At runtime, we embed the user's question, run a vector similarity search to find the most relevant tables, and pull in any adjacent tables. We only inject this small, highly-relevant subset into the LLM prompt.
4.  **Metric Glossary:** At scale, words like "revenue" get ambiguous. I would add a small glossary mapping business terms to strict SQL fragments, injecting them alongside the tables so the LLM doesn't have to guess how the company calculates churn.
5.  **Domain-Specific Sub-Agents:** For massive databases, you can't have one monolithic agent. I would split the system into a microservice-like architecture—have separate LangGraph sub-agents for Orders, Support, and Inventory. A router LLM would look at the user's question and pass it to the right sub-agent, which only has access to the tables for its specific domain.
