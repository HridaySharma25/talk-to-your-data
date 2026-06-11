"""Schema description for the language model.

``get_schema_for_llm`` renders a single, carefully-written Markdown document
describing every table and column, the primary/foreign keys, the join graph,
verified enumerations, and the dataset's known gotchas. This string is the most
important context the model receives when translating questions to SQL, so the
structural facts are introspected live from the database (always accurate) and
the *semantic* one-line descriptions are hand-authored below.

The rendered document is cached for the life of the process.
"""

from __future__ import annotations

from functools import lru_cache

import sqlalchemy as sa

from src import db

# --------------------------------------------------------------------------- #
# Hand-authored semantics (cannot be introspected)                            #
# --------------------------------------------------------------------------- #

# Order in which tables are presented to the model — a logical narrative from
# the order hub outward to dimensions.
_TABLE_ORDER: list[str] = [
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

TABLE_DESCRIPTIONS: dict[str, str] = {
    "orders": "One row per customer order — the hub table linking customers to their items, payments, and reviews.",
    "order_items": "One row per line item within an order (an order with N items has N rows). Product revenue lives here.",
    "order_payments": "One row per payment transaction on an order; a single order may be split across multiple payment rows.",
    "order_reviews": "Customer satisfaction reviews. review_score drives CSAT-style analysis.",
    "products": "Product catalog with physical attributes. Category names are Portuguese — join the translation table for English.",
    "product_category_name_translation": "Lookup mapping each Portuguese product_category_name to its English label.",
    "customers": "Customer records. customer_id is per-order; customer_unique_id identifies the real person across orders.",
    "sellers": "Marketplace sellers who fulfill order items.",
    "geolocation": "Latitude/longitude points per zip-code prefix (many rows per prefix). Use for geographic analysis.",
}

COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "orders": {
        "order_id": "Unique order identifier.",
        "customer_id": "Customer who placed the order.",
        "order_status": "Order lifecycle status; one of: approved, canceled, created, delivered, invoiced, processing, shipped, unavailable. Most revenue analysis filters to 'delivered'.",
        "order_purchase_timestamp": "When the order was placed — the primary time dimension for trends and cohorts.",
        "order_approved_at": "When payment was approved (nullable).",
        "order_delivered_carrier_date": "When the order was handed to the logistics carrier (nullable).",
        "order_delivered_customer_date": "When the order reached the customer (nullable); pair with purchase timestamp for delivery time.",
        "order_estimated_delivery_date": "Delivery date promised to the customer at purchase; compare with the actual delivery date for on-time analysis.",
    },
    "order_items": {
        "order_id": "Order this line item belongs to.",
        "order_item_id": "Sequential line number within the order (1, 2, 3 ...). MAX per order = number of items.",
        "product_id": "Product sold on this line.",
        "seller_id": "Seller fulfilling this line.",
        "shipping_limit_date": "Seller's deadline to hand the item to the carrier.",
        "price": "Item price in BRL (Brazilian Real). SUM(price) = product revenue.",
        "freight_value": "Shipping cost charged for this item, in BRL.",
    },
    "order_payments": {
        "order_id": "Order being paid.",
        "payment_sequential": "Sequence number when an order is paid across multiple transactions.",
        "payment_type": "Payment method; one of: boleto, credit_card, debit_card, not_defined, voucher.",
        "payment_installments": "Number of installments chosen by the customer (1–24).",
        "payment_value": "Transaction amount in BRL. SUM(payment_value) = total paid by the customer (includes freight).",
    },
    "order_reviews": {
        "review_id": "Review identifier (not unique on its own; some reviews recur).",
        "order_id": "Order being reviewed.",
        "review_score": "Customer rating from 1 (worst) to 5 (best). AVG(review_score) or its distribution = satisfaction.",
        "review_comment_title": "Optional free-text review title (often NULL; Portuguese).",
        "review_comment_message": "Optional free-text review body (often NULL; Portuguese).",
        "review_creation_date": "When the review survey was sent to the customer.",
        "review_answer_timestamp": "When the customer submitted the review.",
    },
    "products": {
        "product_id": "Unique product identifier.",
        "product_category_name": "Product category in Portuguese (≈610 NULLs). Join the translation table for the English name.",
        "product_name_length": "Character count of the product name (nullable).",
        "product_description_length": "Character count of the product description (nullable).",
        "product_photos_qty": "Number of published product photos (nullable).",
        "product_weight_g": "Product weight in grams.",
        "product_length_cm": "Package length in centimeters.",
        "product_height_cm": "Package height in centimeters.",
        "product_width_cm": "Package width in centimeters.",
    },
    "product_category_name_translation": {
        "product_category_name": "Portuguese category name (matches products.product_category_name).",
        "product_category_name_english": "English category label — prefer this in results for readability.",
    },
    "customers": {
        "customer_id": "Per-order customer key (a NEW value is issued for each order).",
        "customer_unique_id": "Stable identifier for the real person across orders. Use COUNT(DISTINCT customer_unique_id) to count customers and to find repeat buyers.",
        "customer_zip_code_prefix": "First digits of the customer's zip code (join to geolocation).",
        "customer_city": "Customer city (lowercase, Portuguese).",
        "customer_state": "Customer state as a 2-letter Brazilian code (e.g. SP, RJ, MG).",
    },
    "sellers": {
        "seller_id": "Unique seller identifier.",
        "seller_zip_code_prefix": "First digits of the seller's zip code (join to geolocation).",
        "seller_city": "Seller city (lowercase, Portuguese).",
        "seller_state": "Seller state as a 2-letter Brazilian code.",
    },
    "geolocation": {
        "geolocation_zip_code_prefix": "First digits of a Brazilian zip code; MANY rows per prefix.",
        "geolocation_lat": "Latitude coordinate.",
        "geolocation_lng": "Longitude coordinate.",
        "geolocation_city": "City for the coordinate (lowercase, Portuguese).",
        "geolocation_state": "State for the coordinate (2-letter Brazilian code).",
    },
}

# Logical join graph, surfaced explicitly so the model picks correct keys.
JOIN_PATHS: list[str] = [
    "orders.customer_id = customers.customer_id",
    "order_items.order_id = orders.order_id",
    "order_items.product_id = products.product_id",
    "order_items.seller_id = sellers.seller_id",
    "order_payments.order_id = orders.order_id",
    "order_reviews.order_id = orders.order_id",
    "products.product_category_name = product_category_name_translation.product_category_name",
    "customers.customer_zip_code_prefix = geolocation.geolocation_zip_code_prefix",
    "sellers.seller_zip_code_prefix = geolocation.geolocation_zip_code_prefix",
]

# Durable modelling guidance and dataset gotchas.
SCHEMA_NOTES: list[str] = [
    "All monetary values (price, freight_value, payment_value) are in BRL (Brazilian Real).",
    "Product revenue = SUM(order_items.price). Total amount paid (incl. freight) = SUM(order_payments.payment_value).",
    "Dates are stored as ISO-8601 text. Use SQLite date functions: strftime('%Y', col) for year, strftime('%Y-%m', col) for month, date(col) for the day, and julianday(b) - julianday(a) for differences in days.",
    "Count customers with COUNT(DISTINCT customer_unique_id) — NOT customer_id, which changes per order.",
    "Category names in `products` are Portuguese; join product_category_name_translation and return the English label.",
    "`geolocation` has multiple rows per zip prefix and no primary key — aggregate (e.g. AVG lat/lng) or pick one row when joining to avoid inflating counts.",
    "Not every order has items, payments, or reviews; choose INNER vs LEFT JOIN deliberately.",
    "review_id is not unique (≈814 duplicate rows) and an order can have multiple reviews.",
]


def _type_label(sa_type: sa.types.TypeEngine) -> str:
    """Map a SQLAlchemy column type to a concise SQLite-flavored label.

    Args:
        sa_type: The reflected SQLAlchemy column type.

    Returns:
        One of ``TEXT``, ``INTEGER``, ``REAL``, ``DATETIME``, or the raw type
        string as a fallback.
    """
    name = str(sa_type).upper()
    if "INT" in name:
        return "INTEGER"
    if name in {"FLOAT", "REAL", "NUMERIC", "DECIMAL"} or "FLOAT" in name:
        return "REAL"
    if "DATE" in name or "TIME" in name:
        return "DATETIME"
    if "CHAR" in name or "TEXT" in name or "STRING" in name or "CLOB" in name:
        return "TEXT"
    return name


def _key_label(
    column: str,
    pk_columns: list[str],
    fk_by_column: dict[str, str],
) -> str:
    """Build the 'Key' cell for a column (PK and/or FK reference).

    Args:
        column: Column name.
        pk_columns: Primary-key column names for the table.
        fk_by_column: Mapping of column name -> "table.column" FK target.

    Returns:
        A label such as ``"PK"``, ``"FK -> orders.order_id"``, ``"PK, FK -> ..."``,
        or an empty string.
    """
    parts: list[str] = []
    if column in pk_columns:
        parts.append("PK")
    if column in fk_by_column:
        parts.append(f"FK -> {fk_by_column[column]}")
    return ", ".join(parts)


def _render_table(table_name: str, row_count: int | None) -> str:
    """Render the Markdown block for one table.

    Args:
        table_name: Name of the table to render.
        row_count: Stored row count, or ``None`` to omit it.

    Returns:
        A Markdown section: heading, description, and a column table.
    """
    inspector = db.get_inspector()
    columns = inspector.get_columns(table_name)
    pk_columns = inspector.get_pk_constraint(table_name).get("constrained_columns", [])
    fk_by_column: dict[str, str] = {}
    for fk in inspector.get_foreign_keys(table_name):
        for local_col, remote_col in zip(
            fk["constrained_columns"], fk["referred_columns"]
        ):
            fk_by_column[local_col] = f"{fk['referred_table']}.{remote_col}"

    col_descriptions = COLUMN_DESCRIPTIONS.get(table_name, {})

    count_label = f" ({row_count:,} rows)" if row_count is not None else ""
    lines = [
        f"### `{table_name}`{count_label}",
        TABLE_DESCRIPTIONS.get(table_name, ""),
        "",
        "| Column | Type | Key | Description |",
        "| --- | --- | --- | --- |",
    ]
    for col in columns:
        name = col["name"]
        lines.append(
            f"| {name} "
            f"| {_type_label(col['type'])} "
            f"| {_key_label(name, pk_columns, fk_by_column)} "
            f"| {col_descriptions.get(name, '')} |"
        )
    return "\n".join(lines)


def _row_counts() -> dict[str, int]:
    """Return per-table row counts for display in the schema doc.

    Returns:
        Mapping of table name -> row count. Empty if the query fails.
    """
    counts: dict[str, int] = {}
    engine = db.get_engine()
    try:
        with engine.connect() as conn:
            for table_name in _TABLE_ORDER:
                counts[table_name] = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608 (trusted names)
                ).scalar_one()
    except sa.exc.SQLAlchemyError:
        return {}
    return counts


def _date_range() -> str:
    """Return a one-line description of the order purchase date range.

    Returns:
        A sentence with the min/max purchase dates, or an empty string if the
        query fails.
    """
    try:
        df = db.read_sql(
            "SELECT MIN(order_purchase_timestamp) AS lo, "
            "MAX(order_purchase_timestamp) AS hi FROM orders"
        )
        lo = str(df.loc[0, "lo"])[:10]
        hi = str(df.loc[0, "hi"])[:10]
        return f"Orders span **{lo}** to **{hi}** (2016 and late-2018 are sparse)."
    except (sa.exc.SQLAlchemyError, KeyError, IndexError):
        return ""


@lru_cache(maxsize=1)
def get_schema_for_llm() -> str:
    """Return the cached Markdown schema description for the language model.

    The result is computed once per process: structural facts (columns, types,
    keys) are introspected from the live database and merged with hand-authored
    semantic descriptions, the join graph, dataset facts, and modelling notes.

    Returns:
        A Markdown string suitable for embedding in the NL->SQL system prompt.
    """
    row_counts = _row_counts()

    sections: list[str] = [
        "# Olist E-Commerce Database — Schema",
        "",
        "SQLite database of a Brazilian e-commerce marketplace. Tables, columns, "
        "keys, and relationships follow.",
        "",
        "## Tables",
        "",
    ]
    for table_name in _TABLE_ORDER:
        sections.append(_render_table(table_name, row_counts.get(table_name)))
        sections.append("")

    sections.append("## Join paths")
    sections.append("")
    sections.extend(f"- {path}" for path in JOIN_PATHS)
    sections.append("")

    sections.append("## Dataset facts & modelling notes")
    sections.append("")
    date_range = _date_range()
    if date_range:
        sections.append(f"- {date_range}")
    sections.extend(f"- {note}" for note in SCHEMA_NOTES)
    sections.append("")

    return "\n".join(sections)
