"""Tests for the NL->SQL module's pure helpers (no network calls).

The live model behaviour is exercised separately; these tests cover the
deterministic logic — fence stripping, JSON parsing, the unanswerable sentinel,
and multi-turn content assembly — so they run fast and cost nothing.
"""

from __future__ import annotations

import pytest

from src import config, nl2sql


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("```json\n{\"a\": 1}\n```", '{"a": 1}'),
        ("SELECT 1", "SELECT 1"),
        ("`SELECT 1`", "SELECT 1"),
        ("   SELECT 1   ", "SELECT 1"),
    ],
)
def test_strip_code_fences(raw: str, expected: str) -> None:
    assert nl2sql._strip_code_fences(raw) == expected


def test_parse_response_valid_json() -> None:
    sql, reasoning = nl2sql._parse_response('{"reasoning": "counts orders", "sql": "SELECT COUNT(*) FROM orders"}')
    assert sql == "SELECT COUNT(*) FROM orders"
    assert reasoning == "counts orders"


@pytest.mark.parametrize("payload", ['{"reasoning": "no data", "sql": "NULL"}', '{"reasoning": "x", "sql": null}'])
def test_parse_response_unanswerable_is_none(payload: str) -> None:
    sql, _ = nl2sql._parse_response(payload)
    assert sql is None


def test_parse_response_strips_fences_inside_json() -> None:
    sql, _ = nl2sql._parse_response('{"reasoning": "r", "sql": "```sql\\nSELECT 1\\n```"}')
    assert sql == "SELECT 1"


def test_parse_response_falls_back_to_raw_sql() -> None:
    # Not valid JSON -> treat the (de-fenced) text as the SQL itself.
    sql, reasoning = nl2sql._parse_response("SELECT 1 FROM orders")
    assert sql == "SELECT 1 FROM orders"
    assert reasoning == ""


def test_build_contents_without_history() -> None:
    contents = nl2sql._build_contents("How many orders?", None)
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    assert contents[0]["parts"] == ["How many orders?"]


def test_build_contents_with_history() -> None:
    history = [{"question": "Total orders?", "sql": "SELECT COUNT(*) FROM orders"}]
    contents = nl2sql._build_contents("And in 2018?", history)
    # user(prior) + model(prior) + user(current)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"] == ["Total orders?"]
    assert contents[-1]["parts"] == ["And in 2018?"]


def test_prompt_template_has_schema_placeholder() -> None:
    assert "{{SCHEMA}}" in nl2sql._load_prompt_template()


def test_estimate_cost_is_nonnegative() -> None:
    assert config.estimate_cost_usd(3000, 200) > 0
    assert config.estimate_cost_usd(0, 0) == 0
