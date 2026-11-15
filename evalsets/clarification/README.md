# Clarification eval

Does the agent ask a clarifying question exactly when the question is ambiguous, and answer
directly when it is not? 100 questions against the app's own demo database (`nl2sql_assignment`,
the e-commerce schema in `db/schema.sql`).

```
make eval-clarify LABEL=v1 CLAR_ARGS=--confirm-real-llm          # full run, REAL model, needs approval
make eval-clarify LABEL=dev CLAR_ARGS="--limit 3"               # 3 ambiguous + 3 clear
LLM_PROVIDER=fake make eval-clarify LABEL=smoke                  # wiring check, no model
PYTHONPATH=$PWD/src uv run python evalsets/clarification/run_clarification_eval.py --budget
```

The runner **refuses to use any provider other than `fake` unless `--confirm-real-llm` is passed**.
Output: `results/clarification_<label>.json` and `.md` (self-describing: git SHA, models, prompt
and dataset hashes, per-item records), plus a resumable checkpoint
`results/clarification_<label>.jsonl` (gitignored; errored items are retried on resume).

## Dataset (`questions.yaml`)

* **50 ambiguous**, each with an `ambiguity_type` and a one-line `note` saying why:
  `undefined_metric` 10 ("best customers", "revenue"), `ranking_criterion` 5 ("top 5 customers"),
  `ambiguous_entity` 10 (two customers are named Rahul Menon; Priya Verma is both a customer and
  an agent; context-free references like "that order"), `time_range` 8 ("recently"),
  `threshold` 8 ("large orders"), `column_ambiguity` 9 (amounts, prices, statuses and cities each
  live in several tables).
* **50 clear**: 30 `easy` (explicit table, column and filter) and 20 `near_miss`, which look like
  the ambiguous ones but pin down the missing piece ("top 5 customers **by total completed
  payment amount**", "revenue **defined as** the sum of completed payments").
* No question trips the deterministic prompt-injection regex, so the planner's judgement is what
  is measured. Entities in clear questions are unique in the data (checked by an integration test).

## Scoring

"Asked for clarification" is mechanical: the final status is `needs_clarification`. Positive
class is *ambiguous*.

| metric | definition |
|---|---|
| recall | ambiguous questions that got a clarification / 50 |
| precision | ambiguous questions clarified / all questions clarified |
| F1 | harmonic mean of the two |
| false-clarify rate | clear questions that got a clarification / 50 (over-asking) |

95% CIs: recall and false-clarify rate use `nl2sql.eval.stats.bootstrap_ci` on the per-item 0/1
indicators; precision and F1 depend on both classes so they use a stratified percentile
bootstrap (ambiguous and clear items resampled separately, 2000 resamples, fixed seed). A
resample with zero clarifications has no precision and is skipped for that metric; F1 counts it
as 0. The report also gives recall per ambiguity type and lists the false-clarified and missed
ids. Infrastructure errors (provider outage, `llm_error`) are excluded from every denominator
and reported as `n_errors`.

A clear question that is answered, refused or failed all count as "did not ask"; the report shows
the status mix so a refusal is visible rather than silently scored as correct.

## Groq budget estimate for one full run

Free tier: 1000 requests/day and 200,000 tokens/day per model. The planner prompt for the demo
schema is about 3.8K tokens plus about 0.8K output, so a request is about 5K tokens and the token
cap allows about **40 planner requests per day**; the request cap is never reached. No item is
stopped by the regex guard, so every item costs at least one planner request. One clarification
question or a refusal costs exactly one; an answered question adds a synthesis call only for
results over 50 rows (fast model, separate quota), and failed SQL adds correction calls on the
planner model.

| scenario | planner requests | planner tokens | days (200K tokens/day) |
|---|---|---|---|
| floor: 1 request per question | 100 | 0.50M | 3 |
| expected: 1.1 requests per question | about 110 | about 0.55M | 3 |
| worst: 1 + 3 correction retries on every question | 400 | 2.0M | 10 |

The checkpoint file makes a multi-day run straightforward: re-run the same command and label the
next day. A `--limit 3` smoke run (3 ambiguous + 3 clear) is about 6 requests, 30K tokens.
