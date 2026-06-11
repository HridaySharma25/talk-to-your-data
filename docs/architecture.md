# Architecture

This document explains how a question becomes an answer, component by component.
For the high-level diagram see the [README](../README.md#architecture).

## Request lifecycle

1. **UI (`app/streamlit_app.py`)** captures the question and renders staged
   progress. It owns orchestration and error presentation only — no business
   logic lives here.
2. **Schema context (`src/schema.py`)** supplies the cached markdown the model
   is grounded on.
3. **NL→SQL (`src/nl2sql.py`)** asks Gemini for a JSON object `{reasoning, sql}`.
   `sql = NULL` signals an unanswerable question.
4. **Validation (`src/safety.py::validate_sql`)** is the gate: read-only,
   single-statement, real tables/columns. A failure short-circuits with a
   reason and the offending SQL.
5. **Execution (`src/safety.py::safe_execute`)** runs the validated SQL on the
   read-only engine with a timeout and an injected `LIMIT`.
6. **Visualization (`src/visualize.py`)** and **interpretation
   (`src/interpret.py`)** turn the result into a chart and a narrative.
7. **Logging (`src/logger.py`)** records the whole event to `queries.jsonl`.

## Components

### `config.py` — single source of truth
Every tunable value (paths, model name, temperature, retry policy, row/time
limits, token cost rates) lives here. Business logic never hardcodes these, so
swapping the model or a limit is a one-line change. This decoupling is what made
migrating off the deprecated `gemini-2.0-flash` a trivial edit.

### `db.py` — the read-only data layer
A lazily-created, process-wide SQLAlchemy engine. A `connect` event runs
`PRAGMA query_only = ON` on every connection, so the driver itself rejects any
write — defense in depth beneath the validator. `check_same_thread=False` lets
the query-timeout watchdog interrupt from another thread.

### `schema.py` — the highest-leverage component
`get_schema_for_llm()` merges **introspected structure** (tables, columns,
types, PKs, FKs — always accurate) with **hand-authored semantics** (one-line
column descriptions, the join graph, verified enumerations, the date range, and
modelling gotchas). Caching with `lru_cache` makes it free after the first call.
SQL accuracy is mostly a function of this document's quality.

### `safety.py` — validation + safe execution
- `validate_sql(sql) -> (bool, str)`: ordered checks — non-empty → exactly one
  statement → starts with `SELECT`/`WITH` → no forbidden keywords → tables exist
  → qualified/bare columns exist. The column checker gathers CTE names, aliases,
  function names, and SQL type names to avoid false positives on legitimate
  analytics SQL (window functions, `CAST(... AS REAL)`, aliases reused in
  `ORDER BY`).
- `safe_execute(sql, timeout, row_limit) -> DataFrame`: injects a top-level
  `LIMIT` if absent and runs under a `threading.Timer` watchdog that calls
  `connection.interrupt()` on timeout. Errors surface as typed exceptions
  (`QueryExecutionError`, `QueryTimeoutError`).

### `nl2sql.py` — natural language to SQL
Builds a `GenerativeModel` with the schema-injected system prompt and JSON
response mode (`temperature=0`). The API call is wrapped in a tenacity
exponential-backoff retry for transient errors (`ResourceExhausted`,
`ServiceUnavailable`, …). Output is parsed defensively (JSON first, fence
stripping as a fallback) and the documented `"NULL"` sentinel maps to
`sql=None`. Returns `{sql, reasoning, tokens_in, tokens_out, latency_ms, model,
estimated_cost_usd}`.

### `interpret.py` — result to narrative
Sends the question, the SQL, and a **token-bounded representation** of the
result (shape, dtypes, head, plus `describe()`/value-counts for larger frames —
never the raw data) and returns a 2–3 sentence executive summary. Same retry
pattern.

### `visualize.py` — chart heuristics
`auto_chart(df, question)` chooses by result shape and dtype: 1×1 numeric → KPI
card; datetime + numeric → line; category + numeric → bar; two continuous
numerics → scatter; one dimension + many measures → grouped bar / multi-line;
otherwise a table. Temporal detection is value- and name-aware because SQLite
returns dates and `strftime` periods as text, not datetime dtypes.

### `logger.py` — observability
Appends one JSON object per request to `logs/queries.jsonl` (timestamp,
question, SQL, validation result, row count, latency, tokens, cost, success,
error). Writes are best-effort (never raise into the caller). `summarize_logs()`
provides headline monitoring metrics.

## Design decisions & trade-offs

- **Direct SDK over a framework.** Using `google-generativeai` directly (no
  LangChain) keeps the prompt, parsing, and retry logic explicit and auditable.
- **Validate, then trust a read-only engine.** Two independent layers means a
  gap in one (e.g. an unusual SQL construct the parser mishandles) cannot cause
  data loss — the engine is physically read-only.
- **Schema doc, not fine-tuning.** Accuracy comes from a high-quality grounding
  document, which is cheap to iterate and transparent — not from model training.
- **Column validation favors low false-positives.** A wrongly-rejected valid
  query is worse UX than a hallucinated column slipping through, because the
  latter is caught cleanly at execution time. The validator is tuned
  accordingly and was hardened against real cases found by the eval harness.
- **Errors vs failures in eval.** The harness distinguishes model mistakes from
  infrastructure errors (rate limits), so the reported pass rate measures the
  thing we care about.
