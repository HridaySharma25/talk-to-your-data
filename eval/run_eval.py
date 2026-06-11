"""Evaluation harness for the NL->SQL pipeline.

Runs every question in ``eval/test_questions.json`` through the live pipeline
and scores each on four criteria:

* the safety validator accepts the SQL,
* the SQL matches the question's expected pattern (regex),
* the query executes successfully, and
* the row count falls within the expected range.

Impossible questions invert the test: they pass only if the model correctly
refuses (returns no SQL).

Outputs a timestamped JSON record and a human-readable ``latest_summary.md``
with pass rates by difficulty and category, latency, cost, and every failure.

Run from the project root:

    python eval/run_eval.py
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import config, logger, nl2sql, safety, schema  # noqa: E402

_LOG = logging.getLogger("run_eval")
_PATTERN_FLAGS = re.IGNORECASE | re.DOTALL

# A 15-question representative subset (every category, incl. all 3 impossible
# questions) sized to fit the Gemini free-tier daily request cap. Selected with
# `--subset`; the full 30-question set runs by default.
SUBSET_QUESTION_IDS = frozenset({1, 4, 7, 9, 10, 12, 13, 15, 17, 19, 21, 24, 28, 29, 30})


def evaluate_question(entry: dict, schema_doc: str) -> dict:
    """Run and score a single test question.

    Args:
        entry: A test-question record from ``test_questions.json``.
        schema_doc: The schema description passed to the model.

    Returns:
        A result dict with the outcome, per-check booleans, the generated SQL,
        row count, and request metrics.
    """
    result: dict = {
        "id": entry["id"],
        "question": entry["question"],
        "category": entry["category"],
        "difficulty": entry["difficulty"],
        "passed": False,
        "errored": False,
        "sql": None,
        "reasoning": None,
        "row_count": None,
        "latency_ms": None,
        "tokens_in": None,
        "tokens_out": None,
        "estimated_cost_usd": None,
        "checks": {},
        "error": None,
    }

    try:
        generation = nl2sql.question_to_sql(entry["question"], schema_doc)
    except nl2sql.Nl2SqlError as exc:
        # Transport/quota failure (e.g. rate-limit 429) — not the model's fault.
        result["errored"] = True
        result["error"] = str(exc)
        _log_to_query_log(result)
        return result

    sql = generation["sql"]
    result.update(
        sql=sql,
        reasoning=generation["reasoning"],
        latency_ms=generation["latency_ms"],
        tokens_in=generation["tokens_in"],
        tokens_out=generation["tokens_out"],
        estimated_cost_usd=generation["estimated_cost_usd"],
    )

    if entry["category"] == "impossible":
        refused = sql is None
        result["checks"] = {"refused": refused}
        result["passed"] = refused
        if not refused:
            result["error"] = "Produced SQL for an unanswerable question (hallucination)."
        _log_to_query_log(result)
        return result

    # Answerable question.
    checks: dict = {}
    if sql is None:
        checks["produced_sql"] = False
        result["checks"] = checks
        result["error"] = "Returned NULL for an answerable question."
        _log_to_query_log(result)
        return result

    is_valid, message = safety.validate_sql(sql)
    checks["validation"] = is_valid
    if not is_valid:
        result["error"] = f"Validation failed: {message}"

    pattern = entry.get("expected_sql_pattern")
    checks["pattern"] = bool(re.search(pattern, sql, _PATTERN_FLAGS)) if pattern else True

    executed = False
    range_ok = False
    if is_valid:
        try:
            df = safety.safe_execute(sql)
            result["row_count"] = len(df)
            executed = True
        except safety.SafetyError as exc:
            result["error"] = str(exc)
        checks["executed"] = executed
        if executed:
            value_range = entry.get("expected_row_count_range")
            range_ok = (value_range[0] <= len(df) <= value_range[1]) if value_range else True
            checks["row_count_in_range"] = range_ok

    result["checks"] = checks
    result["passed"] = bool(is_valid and checks["pattern"] and executed and range_ok)
    _log_to_query_log(result)
    return result


def _log_to_query_log(result: dict) -> None:
    """Mirror an eval result into the shared JSONL query log."""
    logger.log_query(
        logger.QueryLogRecord(
            question=result["question"],
            generated_sql=result["sql"],
            validation_passed=result["checks"].get("validation"),
            row_count=result["row_count"],
            latency_ms=result["latency_ms"],
            input_tokens=result["tokens_in"],
            output_tokens=result["tokens_out"],
            estimated_cost_usd=result["estimated_cost_usd"],
            success=result["passed"],
            error_message=result["error"],
        )
    )


def _rate(passed: int, total: int) -> float:
    """Return a rounded pass rate, guarding against division by zero."""
    return round(passed / total, 3) if total else 0.0


def aggregate(results: list[dict]) -> dict:
    """Aggregate per-question results into summary metrics.

    Args:
        results: The list of per-question result dicts.

    Returns:
        A summary dict with overall, by-difficulty, and by-category pass rates,
        average latency, total cost, and total tokens.
    """
    total = len(results)
    errored = sum(1 for r in results if r.get("errored"))
    completed = total - errored
    passed = sum(1 for r in results if r["passed"])

    # Pass rates are computed over completed questions only; errored questions
    # (e.g. rate-limited) are not the model's fault and are excluded.
    by_difficulty: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        if r.get("errored"):
            continue
        for bucket, key in ((by_difficulty, r["difficulty"]), (by_category, r["category"])):
            bucket[key][1] += 1
            if r["passed"]:
                bucket[key][0] += 1

    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    costs = [r["estimated_cost_usd"] for r in results if r["estimated_cost_usd"] is not None]
    tokens_in = sum(r["tokens_in"] or 0 for r in results)
    tokens_out = sum(r["tokens_out"] or 0 for r in results)

    return {
        "total": total,
        "completed": completed,
        "errored": errored,
        "passed": passed,
        "pass_rate": _rate(passed, completed),
        "by_difficulty": {k: {"passed": v[0], "total": v[1], "pass_rate": _rate(*v)} for k, v in by_difficulty.items()},
        "by_category": {k: {"passed": v[0], "total": v[1], "pass_rate": _rate(*v)} for k, v in by_category.items()},
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "total_cost_usd": round(sum(costs), 6),
        "total_tokens_in": tokens_in,
        "total_tokens_out": tokens_out,
    }


def _render_markdown(summary: dict, results: list[dict], model: str, timestamp: str) -> str:
    """Render the human-readable Markdown evaluation report."""
    lines = [
        "# Evaluation Summary",
        "",
        f"- **Run:** {timestamp}",
        f"- **Model:** `{model}`",
        f"- **Pass rate (completed questions):** {summary['passed']}/{summary['completed']} "
        f"({summary['pass_rate'] * 100:.1f}%)",
        f"- **Total questions:** {summary['total']}  ·  "
        f"**Errored / excluded:** {summary['errored']}",
        f"- **Average latency:** {summary['avg_latency_ms']} ms",
        f"- **Total theoretical cost:** ${summary['total_cost_usd']:.6f} "
        f"({summary['total_tokens_in']:,} in / {summary['total_tokens_out']:,} out tokens)",
        "",
        "## Pass rate by difficulty",
        "",
        "| Difficulty | Passed | Total | Pass rate |",
        "| --- | --- | --- | --- |",
    ]
    for key in ("easy", "medium", "hard"):
        if key in summary["by_difficulty"]:
            d = summary["by_difficulty"][key]
            lines.append(f"| {key} | {d['passed']} | {d['total']} | {d['pass_rate'] * 100:.0f}% |")

    lines += ["", "## Pass rate by category", "", "| Category | Passed | Total | Pass rate |", "| --- | --- | --- | --- |"]
    for key in sorted(summary["by_category"]):
        c = summary["by_category"][key]
        lines.append(f"| {key} | {c['passed']} | {c['total']} | {c['pass_rate'] * 100:.0f}% |")

    failures = [r for r in results if not r["passed"] and not r.get("errored")]
    lines += ["", f"## Failures ({len(failures)})", ""]
    if not failures:
        lines.append("None — all completed questions passed. 🎉")
    for r in failures:
        failed_checks = [k for k, v in r["checks"].items() if v is False]
        error = (r["error"] or "n/a").splitlines()[0][:160]
        lines += [
            f"### Q{r['id']} ({r['difficulty']}/{r['category']}) — {r['question']}",
            f"- Failed checks: {', '.join(failed_checks) or 'n/a'}",
            f"- Row count: {r['row_count']}",
            f"- Error: {error}",
            f"- SQL: `{(r['sql'] or 'None')}`",
            "",
        ]

    errored = [r for r in results if r.get("errored")]
    if errored:
        lines += [
            "",
            f"## Errored — excluded from scoring ({len(errored)})",
            "",
            "Did not complete due to transport/quota errors (e.g. free-tier rate "
            "limiting), not model mistakes:",
            "",
        ]
        lines += [f"- Q{r['id']} ({r['difficulty']}/{r['category']}) — {r['question']}" for r in errored]
        lines.append("")

    return "\n".join(lines) + "\n"


def _render_readme_block(summary: dict, model: str, timestamp: str) -> str:
    """Render the compact results block injected into the README."""
    avg = summary["avg_latency_ms"]
    avg_str = f"{avg:.0f} ms" if avg is not None else "n/a"
    lines = [
        f"**Latest run:** {timestamp} · model `{model}`  ",
        f"**Pass rate (completed):** {summary['passed']}/{summary['completed']} "
        f"({summary['pass_rate'] * 100:.1f}%) · {summary['errored']} errored/excluded · "
        f"avg latency {avg_str} · est. cost ${summary['total_cost_usd']:.4f}",
        "",
        "| Difficulty | Pass rate |",
        "| --- | --- |",
    ]
    for key in ("easy", "medium", "hard"):
        if key in summary["by_difficulty"]:
            d = summary["by_difficulty"][key]
            lines.append(f"| {key} | {d['passed']}/{d['total']} ({d['pass_rate'] * 100:.0f}%) |")
    lines.append("")
    lines.append("Full report: [`eval/results/latest_summary.md`](eval/results/latest_summary.md)")
    return "\n".join(lines)


def _update_readme(summary: dict, model: str, timestamp: str) -> None:
    """Inject the latest results into the README between the marker comments.

    No-op if the README or its markers are absent, so the harness never depends
    on the README's presence.
    """
    readme = config.PROJECT_ROOT / "README.md"
    start, end = "<!-- EVAL_RESULTS_START -->", "<!-- EVAL_RESULTS_END -->"
    if not readme.exists():
        return
    text = readme.read_text(encoding="utf-8")
    if start not in text or end not in text:
        return
    block = _render_readme_block(summary, model, timestamp)
    before, after = text.split(start)[0], text.split(end, 1)[1]
    readme.write_text(f"{before}{start}\n{block}\n{end}{after}", encoding="utf-8")
    _LOG.info("Updated README results block.")


def main(argv: list[str] | None = None) -> None:
    """Run the evaluation and write the JSON + Markdown reports.

    Args:
        argv: Optional CLI args (for testing). ``--subset`` runs only the
            free-tier-friendly subset of questions.
    """
    parser = argparse.ArgumentParser(description="Run the NL->SQL evaluation harness.")
    parser.add_argument(
        "--subset",
        action="store_true",
        help="Run only the ~15-question representative subset (fits free-tier 20/day cap).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")

    questions = json.loads(config.TEST_QUESTIONS_PATH.read_text(encoding="utf-8"))
    if args.subset:
        questions = [q for q in questions if q["id"] in SUBSET_QUESTION_IDS]
    schema_doc = schema.get_schema_for_llm()
    _LOG.info(
        "Evaluating %d questions (%s) with model %s ...",
        len(questions), "subset" if args.subset else "full set", config.GEMINI_MODEL,
    )

    results: list[dict] = []
    for i, entry in enumerate(questions, start=1):
        result = evaluate_question(entry, schema_doc)
        results.append(result)
        _LOG.info(
            "[%2d/%d] %s  Q%d: %s",
            i, len(questions), "PASS" if result["passed"] else "FAIL",
            result["id"], result["question"][:55],
        )
        if i < len(questions):
            time.sleep(config.EVAL_SLEEP_BETWEEN_QUESTIONS_S)

    summary = aggregate(results)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    config.EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_path = config.EVAL_RESULTS_DIR / f"run_{file_stamp}.json"
    run_path.write_text(
        json.dumps(
            {"timestamp": timestamp, "model": config.GEMINI_MODEL, "summary": summary, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_path = config.EVAL_RESULTS_DIR / "latest_summary.md"
    summary_path.write_text(_render_markdown(summary, results, config.GEMINI_MODEL, timestamp), encoding="utf-8")
    _update_readme(summary, config.GEMINI_MODEL, timestamp)

    _LOG.info("-" * 60)
    _LOG.info(
        "DONE: %d/%d completed passed (%.1f%%) | %d errored | avg %.0f ms | cost $%.6f",
        summary["passed"], summary["completed"], summary["pass_rate"] * 100,
        summary["errored"], summary["avg_latency_ms"] or 0, summary["total_cost_usd"],
    )
    _LOG.info("Reports: %s  and  %s", run_path.name, summary_path.name)


if __name__ == "__main__":
    main()
