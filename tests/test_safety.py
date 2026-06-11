"""Tests for the SQL safety layer (validation, limit injection, execution)."""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa

from src import config, db, safety

# --------------------------------------------------------------------------- #
# validate_sql — queries that MUST be accepted                                #
# --------------------------------------------------------------------------- #
VALID_QUERIES: list[str] = [
    "SELECT COUNT(*) FROM orders",
    "SELECT order_status, COUNT(*) AS n FROM orders GROUP BY order_status ORDER BY n DESC",
    # 3+ table join with aliases and qualified columns
    "SELECT t.product_category_name_english AS category, ROUND(SUM(oi.price), 2) AS revenue "
    "FROM order_items oi "
    "JOIN orders o ON o.order_id = oi.order_id "
    "JOIN products p ON p.product_id = oi.product_id "
    "JOIN product_category_name_translation t ON t.product_category_name = p.product_category_name "
    "WHERE o.order_status = 'delivered' GROUP BY category ORDER BY revenue DESC LIMIT 5",
    # window function
    "SELECT oi.order_id, oi.price, "
    "ROW_NUMBER() OVER (PARTITION BY oi.order_id ORDER BY oi.price DESC) AS rn FROM order_items oi",
    # CTE
    "WITH monthly AS (SELECT strftime('%Y-%m', o.order_purchase_timestamp) AS ym, "
    "SUM(oi.price) AS rev FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
    "GROUP BY ym) SELECT ym, rev FROM monthly ORDER BY ym",
    # distinct customers via the correct identifier
    "SELECT COUNT(DISTINCT c.customer_unique_id) FROM customers c",
    # old-style comma join
    "SELECT o.order_id FROM orders o, customers c WHERE o.customer_id = c.customer_id LIMIT 5",
    # subquery whose derived column is referenced via its alias
    "SELECT sub.ym FROM (SELECT strftime('%Y-%m', order_purchase_timestamp) AS ym FROM orders) sub",
    # CASE expression with alias reused in GROUP BY
    "SELECT CASE WHEN review_score >= 4 THEN 'good' ELSE 'bad' END AS bucket, "
    "COUNT(*) AS n FROM order_reviews GROUP BY bucket",
    "SELECT * FROM products WHERE product_weight_g > 1000",
    # Percentage via CAST(... AS REAL) — the SQLite type name must not be
    # mistaken for an unknown column (regression test).
    "SELECT ROUND(CAST(SUM(CASE WHEN o.order_status = 'delivered' THEN 1 ELSE 0 END) AS REAL) "
    "* 100.0 / COUNT(o.order_id), 2) AS delivered_pct FROM orders o",
]

# --------------------------------------------------------------------------- #
# validate_sql — queries that MUST be rejected (with a reason substring)      #
# --------------------------------------------------------------------------- #
INVALID_QUERIES: list[tuple[str, str]] = [
    ("DROP TABLE orders", "select"),
    ("DELETE FROM orders", "select"),
    ("UPDATE orders SET order_status = 'x'", "select"),
    ("INSERT INTO orders (order_id) VALUES ('x')", "select"),
    ("ALTER TABLE orders ADD COLUMN x INT", "select"),
    ("PRAGMA table_info(orders)", "select"),
    ("SELECT * FROM orders; DROP TABLE orders", "single"),
    ("SELECT 1 AS a; SELECT 2 AS b", "single"),
    ("WITH x AS (SELECT 1 AS a) DELETE FROM orders", "forbidden"),
    ("SELECT * FROM nonexistent_table", "table"),
    ("SELECT o.fake_column FROM orders o", "column"),
    ("SELECT totally_made_up FROM orders", "column"),
    ("", "empty"),
    ("    ", "empty"),
]


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_validate_accepts_valid_queries(sql: str) -> None:
    is_valid, message = safety.validate_sql(sql)
    assert is_valid, f"Expected valid, got rejection: {message}\nSQL: {sql}"


@pytest.mark.parametrize("sql,reason", INVALID_QUERIES)
def test_validate_rejects_invalid_queries(sql: str, reason: str) -> None:
    is_valid, message = safety.validate_sql(sql)
    assert not is_valid, f"Expected rejection but accepted:\nSQL: {sql}"
    assert reason.lower() in message.lower(), f"Reason '{reason}' not in '{message}'"


def test_validate_returns_tuple_shape() -> None:
    result = safety.validate_sql("SELECT 1 AS x FROM orders")
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool) and isinstance(result[1], str)


# --------------------------------------------------------------------------- #
# ensure_limit                                                                #
# --------------------------------------------------------------------------- #
def test_ensure_limit_appends_when_absent() -> None:
    out = safety.ensure_limit("SELECT * FROM orders")
    assert out == f"SELECT * FROM orders LIMIT {config.SQL_ROW_LIMIT}"


def test_ensure_limit_respects_existing_limit() -> None:
    assert safety.ensure_limit("SELECT * FROM orders LIMIT 5") == "SELECT * FROM orders LIMIT 5"


def test_ensure_limit_respects_limit_offset() -> None:
    sql = "SELECT * FROM orders ORDER BY order_id LIMIT 5 OFFSET 2"
    assert safety.ensure_limit(sql) == sql


def test_ensure_limit_strips_trailing_semicolon() -> None:
    assert safety.ensure_limit("SELECT * FROM orders;") == (
        f"SELECT * FROM orders LIMIT {config.SQL_ROW_LIMIT}"
    )


def test_ensure_limit_custom_row_limit() -> None:
    assert safety.ensure_limit("SELECT * FROM orders", row_limit=10) == (
        "SELECT * FROM orders LIMIT 10"
    )


# --------------------------------------------------------------------------- #
# safe_execute                                                                #
# --------------------------------------------------------------------------- #
def test_safe_execute_returns_expected_rows() -> None:
    df = safety.safe_execute("SELECT COUNT(*) AS n FROM orders")
    assert df.iloc[0]["n"] == 99_441


def test_safe_execute_injects_row_limit() -> None:
    df = safety.safe_execute("SELECT order_id FROM orders", row_limit=7)
    assert len(df) == 7


def test_safe_execute_raises_on_bad_column() -> None:
    with pytest.raises(safety.QueryExecutionError):
        safety.safe_execute("SELECT nope FROM orders")


def test_safe_execute_rejects_writes() -> None:
    # Defence in depth: the read-only engine blocks writes even if they reach
    # execution. ensure_limit/SQLite raise -> wrapped as QueryExecutionError.
    with pytest.raises(safety.QueryExecutionError):
        safety.safe_execute("DELETE FROM orders")


def test_safe_execute_times_out() -> None:
    # A cross join over the 1M-row geolocation table cannot finish in 2s.
    start = time.time()
    with pytest.raises(safety.QueryTimeoutError):
        safety.safe_execute("SELECT COUNT(*) FROM geolocation a, geolocation b", timeout=2)
    assert time.time() - start < 10  # interrupted promptly, not run to completion


# --------------------------------------------------------------------------- #
# Read-only engine guarantee                                                  #
# --------------------------------------------------------------------------- #
def test_engine_is_read_only() -> None:
    with pytest.raises(sa.exc.OperationalError):
        with db.get_engine().begin() as conn:
            conn.execute(sa.text("CREATE TABLE _should_not_exist (x INTEGER)"))
