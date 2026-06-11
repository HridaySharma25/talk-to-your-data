"""Structured query logging.

Every question that flows through the pipeline is appended as one JSON object
to ``logs/queries.jsonl`` (JSON Lines). This append-only event log is the
foundation for the evaluation harness and for the "how would you monitor this
in production" story: latency, token usage, cost, validation outcomes, and
errors are all captured per request and are trivially aggregatable.

Logging never raises into the caller — a failure to write a log line must not
break a user's query — so write errors are swallowed and surfaced via the
standard library logger instead.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean

from src import config

_LOG = logging.getLogger("ttyd.querylog")
_WRITE_LOCK = threading.Lock()


@dataclass
class QueryLogRecord:
    """One pipeline event for the query log.

    Attributes:
        question: The natural-language question asked.
        generated_sql: The SQL produced by the model (``None`` if unanswerable).
        validation_passed: Whether the safety validator accepted the SQL.
        validation_message: The validator's reason/explanation.
        row_count: Number of rows the query returned.
        latency_ms: End-to-end latency for the request, in milliseconds.
        input_tokens: Prompt tokens billed across LLM calls.
        output_tokens: Completion tokens billed across LLM calls.
        estimated_cost_usd: Theoretical cost of the LLM calls.
        success: Whether the request completed end to end without error.
        error_message: Error text if the request failed.
        timestamp: ISO-8601 UTC timestamp; set automatically at write time.
    """

    question: str
    generated_sql: str | None = None
    validation_passed: bool | None = None
    validation_message: str | None = None
    row_count: int | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    success: bool = False
    error_message: str | None = None
    timestamp: str | None = field(default=None)

    def to_dict(self) -> dict:
        """Return the record as an ordered, JSON-serializable dict.

        Numeric fields are coerced to native Python types so values passed in
        as numpy scalars (e.g. ``len(df)`` artifacts) serialize as JSON numbers
        rather than strings.
        """

        def _opt_int(value: object) -> int | None:
            return int(value) if value is not None else None

        def _opt_float(value: object) -> float | None:
            return float(value) if value is not None else None

        return {
            "timestamp": self.timestamp,
            "question": self.question,
            "generated_sql": self.generated_sql,
            "validation_passed": self.validation_passed,
            "validation_message": self.validation_message,
            "row_count": _opt_int(self.row_count),
            "latency_ms": _opt_int(self.latency_ms),
            "input_tokens": _opt_int(self.input_tokens),
            "output_tokens": _opt_int(self.output_tokens),
            "estimated_cost_usd": _opt_float(self.estimated_cost_usd),
            "success": bool(self.success),
            "error_message": self.error_message,
        }


def log_query(record: QueryLogRecord) -> None:
    """Append a query record to the JSONL log.

    Sets the timestamp at write time. Any I/O error is logged and swallowed so
    that logging never breaks the caller's request.

    Args:
        record: The event to persist.
    """
    if record.timestamp is None:
        record.timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = json.dumps(record.to_dict(), ensure_ascii=False, default=str)
    try:
        with _WRITE_LOCK:
            config.QUERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with config.QUERY_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        _LOG.warning("Failed to write query log: %s", exc)


def read_logs(limit: int | None = None) -> list[dict]:
    """Read logged query records, oldest first.

    Args:
        limit: If given, return only the most recent ``limit`` records.

    Returns:
        A list of record dicts. Empty if the log does not exist. Corrupt lines
        are skipped.
    """
    if not config.QUERY_LOG_PATH.exists():
        return []
    records: list[dict] = []
    for line in config.QUERY_LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-limit:] if limit else records


def summarize_logs() -> dict:
    """Aggregate the query log into headline monitoring metrics.

    Returns:
        A dict with total query count, success rate, average latency, and total
        estimated cost. Zeroed when the log is empty.
    """
    records = read_logs()
    total = len(records)
    if total == 0:
        return {
            "total_queries": 0,
            "success_rate": None,
            "avg_latency_ms": None,
            "total_cost_usd": 0.0,
        }
    successes = sum(1 for r in records if r.get("success"))
    latencies = [r["latency_ms"] for r in records if r.get("latency_ms") is not None]
    costs = [r["estimated_cost_usd"] for r in records if r.get("estimated_cost_usd") is not None]
    return {
        "total_queries": total,
        "success_rate": round(successes / total, 3),
        "avg_latency_ms": round(mean(latencies), 1) if latencies else None,
        "total_cost_usd": round(sum(costs), 6),
    }
