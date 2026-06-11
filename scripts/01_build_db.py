"""Build the Olist SQLite database from the raw CSV files.

Reads the nine Olist CSVs from ``data/raw/``, normalizes column names and
types, creates typed tables with primary keys, foreign keys, and indexes on
the common join keys, then loads the data and writes ``data/olist.db``.

The build is idempotent: any existing database file is deleted and rebuilt
from scratch. After loading, the stored row counts are verified against the
dataset's published totals so a partial or corrupt load fails loudly.

Run from the project root:

    python scripts/01_build_db.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import Engine

# Make the `src` package importable when this file is run as a standalone
# script (i.e. `python scripts/01_build_db.py`).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import config  # noqa: E402  (import after sys.path manipulation)

logger = logging.getLogger("build_db")

# Timestamp format shared by every date column in the Olist CSVs.
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# --------------------------------------------------------------------------- #
# Schema definition (SQLAlchemy Core)                                         #
# Foreign keys are declared for documentation and introspection; SQLite does  #
# not enforce them unless `PRAGMA foreign_keys=ON`, which we deliberately      #
# leave off during the bulk load because the public dataset has a few known   #
# referential-integrity gaps (e.g. categories absent from the translation     #
# table). Indexes cover the join keys queries rely on.                         #
# --------------------------------------------------------------------------- #
metadata = MetaData()

customers = Table(
    "customers",
    metadata,
    Column("customer_id", String, primary_key=True),
    Column("customer_unique_id", String, nullable=False),
    Column("customer_zip_code_prefix", Integer, nullable=False),
    Column("customer_city", String, nullable=False),
    Column("customer_state", String, nullable=False),
    Index("ix_customers_unique_id", "customer_unique_id"),
    Index("ix_customers_zip", "customer_zip_code_prefix"),
)

geolocation = Table(
    # No natural primary key: a zip-code prefix maps to many lat/lng rows.
    "geolocation",
    metadata,
    Column("geolocation_zip_code_prefix", Integer, nullable=False),
    Column("geolocation_lat", Float, nullable=False),
    Column("geolocation_lng", Float, nullable=False),
    Column("geolocation_city", String, nullable=False),
    Column("geolocation_state", String, nullable=False),
    Index("ix_geolocation_zip", "geolocation_zip_code_prefix"),
)

sellers = Table(
    "sellers",
    metadata,
    Column("seller_id", String, primary_key=True),
    Column("seller_zip_code_prefix", Integer, nullable=False),
    Column("seller_city", String, nullable=False),
    Column("seller_state", String, nullable=False),
    Index("ix_sellers_zip", "seller_zip_code_prefix"),
)

product_category_name_translation = Table(
    "product_category_name_translation",
    metadata,
    Column("product_category_name", String, primary_key=True),
    Column("product_category_name_english", String, nullable=False),
)

products = Table(
    "products",
    metadata,
    Column("product_id", String, primary_key=True),
    Column(
        "product_category_name",
        String,
        ForeignKey("product_category_name_translation.product_category_name"),
    ),
    # Renamed from the source's misspelled `*_lenght` columns.
    Column("product_name_length", Integer),
    Column("product_description_length", Integer),
    Column("product_photos_qty", Integer),
    Column("product_weight_g", Float),
    Column("product_length_cm", Float),
    Column("product_height_cm", Float),
    Column("product_width_cm", Float),
    Index("ix_products_category", "product_category_name"),
)

orders = Table(
    "orders",
    metadata,
    Column("order_id", String, primary_key=True),
    Column(
        "customer_id",
        String,
        ForeignKey("customers.customer_id"),
        nullable=False,
    ),
    Column("order_status", String, nullable=False),
    Column("order_purchase_timestamp", DateTime, nullable=False),
    Column("order_approved_at", DateTime),
    Column("order_delivered_carrier_date", DateTime),
    Column("order_delivered_customer_date", DateTime),
    Column("order_estimated_delivery_date", DateTime, nullable=False),
    Index("ix_orders_customer_id", "customer_id"),
    Index("ix_orders_purchase_ts", "order_purchase_timestamp"),
    Index("ix_orders_status", "order_status"),
)

order_items = Table(
    "order_items",
    metadata,
    Column("order_id", String, ForeignKey("orders.order_id"), primary_key=True),
    Column("order_item_id", Integer, primary_key=True),
    Column("product_id", String, ForeignKey("products.product_id"), nullable=False),
    Column("seller_id", String, ForeignKey("sellers.seller_id"), nullable=False),
    Column("shipping_limit_date", DateTime, nullable=False),
    Column("price", Float, nullable=False),
    Column("freight_value", Float, nullable=False),
    Index("ix_order_items_product_id", "product_id"),
    Index("ix_order_items_seller_id", "seller_id"),
)

order_payments = Table(
    "order_payments",
    metadata,
    Column("order_id", String, ForeignKey("orders.order_id"), primary_key=True),
    Column("payment_sequential", Integer, primary_key=True),
    Column("payment_type", String, nullable=False),
    Column("payment_installments", Integer, nullable=False),
    Column("payment_value", Float, nullable=False),
)

order_reviews = Table(
    # Composite PK: review_id alone has 814 duplicate rows in the source data.
    "order_reviews",
    metadata,
    Column("review_id", String, primary_key=True),
    Column("order_id", String, ForeignKey("orders.order_id"), primary_key=True),
    Column("review_score", Integer, nullable=False),
    Column("review_comment_title", Text),
    Column("review_comment_message", Text),
    Column("review_creation_date", DateTime, nullable=False),
    Column("review_answer_timestamp", DateTime, nullable=False),
    # order_id is the *second* PK column, so it is not covered by the PK index.
    Index("ix_order_reviews_order_id", "order_id"),
)

# --------------------------------------------------------------------------- #
# Load plan: (csv_filename, table, date_columns, nullable_int_columns,         #
#             column_rename_map). Ordered parents-before-children for tidiness #
# (load order is otherwise irrelevant with FK enforcement off).                #
# --------------------------------------------------------------------------- #
LOAD_PLAN: list[tuple[str, Table, list[str], list[str], dict[str, str]]] = [
    ("product_category_name_translation.csv", product_category_name_translation, [], [], {}),
    ("olist_customers_dataset.csv", customers, [], [], {}),
    ("olist_geolocation_dataset.csv", geolocation, [], [], {}),
    ("olist_sellers_dataset.csv", sellers, [], [], {}),
    (
        "olist_products_dataset.csv",
        products,
        [],
        ["product_name_length", "product_description_length", "product_photos_qty"],
        {
            "product_name_lenght": "product_name_length",
            "product_description_lenght": "product_description_length",
        },
    ),
    (
        "olist_orders_dataset.csv",
        orders,
        [
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ],
        [],
        {},
    ),
    ("olist_order_items_dataset.csv", order_items, ["shipping_limit_date"], [], {}),
    ("olist_order_payments_dataset.csv", order_payments, [], [], {}),
    (
        "olist_order_reviews_dataset.csv",
        order_reviews,
        ["review_creation_date", "review_answer_timestamp"],
        [],
        {},
    ),
]

# Published Olist row counts; the build self-validates against these.
EXPECTED_ROW_COUNTS: dict[str, int] = {
    "product_category_name_translation": 71,
    "customers": 99_441,
    "geolocation": 1_000_163,
    "sellers": 3_095,
    "products": 32_951,
    "orders": 99_441,
    "order_items": 112_650,
    "order_payments": 103_886,
    "order_reviews": 99_224,
}


def load_table(
    engine: Engine,
    csv_path: Path,
    table: Table,
    date_columns: list[str],
    int_columns: list[str],
    rename_map: dict[str, str],
) -> int:
    """Load a single CSV into a pre-created table.

    Reads the CSV with pandas, applies the column renames, parses date columns
    to datetimes, casts the given columns to a nullable integer type, and
    appends the rows into the already-created table (so the table's primary
    keys, foreign keys, and indexes are preserved).

    Args:
        engine: SQLAlchemy engine bound to the target database.
        csv_path: Path to the source CSV file.
        table: The SQLAlchemy table to append into.
        date_columns: Columns to parse as ``%Y-%m-%d %H:%M:%S`` datetimes.
        int_columns: Float-typed count columns to cast to nullable ``Int64``.
        rename_map: Mapping of source column name -> normalized column name.

    Returns:
        The number of rows loaded from the CSV.
    """
    df = pd.read_csv(csv_path)
    if rename_map:
        df = df.rename(columns=rename_map)
    for col in date_columns:
        df[col] = pd.to_datetime(df[col], format=_DATE_FORMAT, errors="coerce")
    for col in int_columns:
        # Nullable integer so NaN counts become SQL NULL rather than floats.
        df[col] = df[col].astype("Int64")
    df.to_sql(
        table.name,
        engine,
        if_exists="append",
        index=False,
        chunksize=config.DB_LOAD_CHUNKSIZE,
    )
    return len(df)


def verify_counts(engine: Engine) -> dict[str, int]:
    """Count the rows actually stored in each table.

    Args:
        engine: SQLAlchemy engine bound to the built database.

    Returns:
        Mapping of table name -> stored row count.
    """
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            counts[table.name] = conn.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
    return counts


def main() -> None:
    """Build (or rebuild) the Olist SQLite database end to end."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Fail fast if any source CSV is missing.
    missing = [
        name for name, *_ in LOAD_PLAN if not (config.RAW_DATA_DIR / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"Missing source CSV(s) in {config.RAW_DATA_DIR}: {', '.join(missing)}"
        )

    # Idempotency: start from a clean file every time.
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        logger.info("Removing existing database: %s", config.DB_PATH)
        config.DB_PATH.unlink()

    engine = create_engine(config.DB_URL)
    try:
        logger.info("Creating schema (%d tables)...", len(metadata.tables))
        metadata.create_all(engine)

        for name, table, date_cols, int_cols, rename_map in LOAD_PLAN:
            rows = load_table(
                engine, config.RAW_DATA_DIR / name, table, date_cols, int_cols, rename_map
            )
            logger.info("Loaded %-36s %9s rows", table.name, f"{rows:,}")

        # Refresh the query planner's statistics for better plans at query time.
        with engine.begin() as conn:
            conn.execute(text("ANALYZE"))

        counts = verify_counts(engine)
    finally:
        engine.dispose()

    # Report and validate.
    logger.info("-" * 56)
    logger.info("%-36s %12s", "TABLE", "ROWS")
    logger.info("-" * 56)
    for table_name, count in counts.items():
        logger.info("%-36s %12s", table_name, f"{count:,}")
    logger.info("-" * 56)
    logger.info("%-36s %12s", "TOTAL", f"{sum(counts.values()):,}")

    mismatches = {
        name: (counts.get(name), expected)
        for name, expected in EXPECTED_ROW_COUNTS.items()
        if counts.get(name) != expected
    }
    if mismatches:
        for name, (actual, expected) in mismatches.items():
            logger.error("Row count mismatch in %s: got %s, expected %s", name, actual, expected)
        raise SystemExit(1)

    logger.info("All row counts match published Olist totals. Database: %s", config.DB_PATH)


if __name__ == "__main__":
    main()
