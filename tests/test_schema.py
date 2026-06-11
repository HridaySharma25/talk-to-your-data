"""Tests for the schema description and database introspection layer."""

from __future__ import annotations

import pytest

from src import db, schema

EXPECTED_TABLES = [
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
    "products",
    "product_category_name_translation",
    "customers",
    "sellers",
    "geolocation",
]


@pytest.fixture(scope="module")
def schema_doc() -> str:
    return schema.get_schema_for_llm()


def test_schema_doc_is_nonempty_string(schema_doc: str) -> None:
    assert isinstance(schema_doc, str)
    assert len(schema_doc) > 1_000


@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_schema_doc_mentions_every_table(schema_doc: str, table: str) -> None:
    assert table in schema_doc


@pytest.mark.parametrize(
    "column",
    [
        "customer_unique_id",
        "order_purchase_timestamp",
        "product_category_name_english",
        "review_score",
        "payment_value",
        "freight_value",
    ],
)
def test_schema_doc_mentions_key_columns(schema_doc: str, column: str) -> None:
    assert column in schema_doc


def test_schema_doc_includes_guidance(schema_doc: str) -> None:
    # The high-leverage modelling hints must be present for SQL quality.
    assert "BRL" in schema_doc
    assert "strftime" in schema_doc
    assert "COUNT(DISTINCT customer_unique_id)" in schema_doc
    assert "## Join paths" in schema_doc
    assert "PK" in schema_doc


def test_schema_doc_is_cached() -> None:
    assert schema.get_schema_for_llm() is schema.get_schema_for_llm()


def test_inspector_reports_all_tables() -> None:
    names = set(db.get_inspector().get_table_names())
    assert names == set(EXPECTED_TABLES)


def test_composite_primary_keys() -> None:
    inspector = db.get_inspector()
    assert inspector.get_pk_constraint("order_items")["constrained_columns"] == [
        "order_id",
        "order_item_id",
    ]
    assert inspector.get_pk_constraint("order_reviews")["constrained_columns"] == [
        "review_id",
        "order_id",
    ]


def test_foreign_keys_present() -> None:
    fks = db.get_inspector().get_foreign_keys("order_items")
    referred = {fk["referred_table"] for fk in fks}
    assert {"orders", "products", "sellers"} <= referred
