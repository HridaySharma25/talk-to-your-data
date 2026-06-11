"""Database access layer.

Provides a single, lazily-created SQLAlchemy engine bound to the Olist SQLite
database, plus light helpers for introspection and trusted read queries.

The engine opens every connection in read-only mode (``PRAGMA query_only=ON``)
as defence in depth: even if the SQL safety validator were bypassed, the
driver itself rejects any statement that would mutate data. ``check_same_thread``
is disabled so a watchdog thread can interrupt a long-running query (see
``src.safety.safe_execute``).
"""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine, Inspector

from src import config

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the shared, read-only SQLAlchemy engine (created on first use).

    Returns:
        A process-wide singleton :class:`~sqlalchemy.engine.Engine` whose
        connections are forced read-only via ``PRAGMA query_only=ON``.
    """
    global _engine
    if _engine is None:
        _engine = sa.create_engine(
            config.DB_URL,
            # Allow a separate thread to call connection.interrupt() for timeouts.
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _enforce_read_only(dbapi_connection, _record) -> None:  # noqa: ANN001
            """Force each new connection into read-only mode."""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only = ON;")
            cursor.close()

    return _engine


def get_inspector() -> Inspector:
    """Return a SQLAlchemy inspector for the database.

    Returns:
        An :class:`~sqlalchemy.engine.Inspector` bound to the shared engine,
        used to read tables, columns, primary keys, and foreign keys.
    """
    return sa.inspect(get_engine())


def read_sql(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Execute a trusted read query and return the result as a DataFrame.

    This helper performs no validation and is intended for internal,
    developer-authored queries (e.g. schema enrichment). User-supplied SQL must
    always go through :func:`src.safety.safe_execute` instead.

    Args:
        sql: A read-only SQL statement.
        params: Optional bound parameters for the statement.

    Returns:
        The query result as a pandas DataFrame.
    """
    with get_engine().connect() as conn:
        return pd.read_sql(sa.text(sql), conn, params=params)


def dispose_engine() -> None:
    """Dispose of the shared engine and reset it.

    Useful in tests and scripts to release file handles on the SQLite database
    so it can be rebuilt or removed.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
