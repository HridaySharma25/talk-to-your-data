"""SQL safety layer.

Two responsibilities:

1. :func:`validate_sql` — a *gate* run before any model-generated SQL touches
   the database. It enforces hard security guarantees (single statement,
   read-only ``SELECT``/``WITH`` only, no DML/DDL) and best-effort schema
   checks (referenced tables and qualified columns must exist).

2. :func:`safe_execute` — executes a query with a hard wall-clock timeout and
   an automatically injected ``LIMIT`` so a runaway or huge result set cannot
   exhaust resources.

These are defence in depth on top of the read-only engine in :mod:`src.db`:
even if a check here were imperfect, the driver itself rejects writes.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache

import pandas as pd
import sqlalchemy as sa
import sqlparse
from sqlparse.sql import Identifier
from sqlparse.tokens import Punctuation, Wildcard

from src import config, db

# Statement keywords that must never appear in a user query. SELECT/UPDATE/etc.
# DML and DDL writes are caught here; INSERT also covers "INSERT OR REPLACE".
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
        "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "GRANT", "REVOKE",
        "RENAME", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
    }
)

# Clause keywords that end the FROM clause (used when scanning for tables).
_FROM_BOUNDARY: frozenset[str] = frozenset(
    {"WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT",
     "EXCEPT", "WINDOW", "ON", "USING", "SELECT"}
)

# Common SQLite function names, kept out of the "unknown column" check in case
# sqlparse tags one as a bare name rather than a function.
_SQL_FUNCTIONS: frozenset[str] = frozenset(
    {"count", "sum", "avg", "min", "max", "round", "abs", "coalesce", "ifnull",
     "nullif", "cast", "length", "lower", "upper", "trim", "substr", "replace",
     "strftime", "date", "datetime", "time", "julianday", "row_number", "rank",
     "dense_rank", "ntile", "lag", "lead", "first_value", "last_value", "total",
     "group_concat", "instr", "printf", "iif", "over", "partition"}
)

# SQLite type names that appear as bare identifiers inside CAST(x AS <type>).
# Without this, e.g. CAST(... AS REAL) would flag "real" as an unknown column.
_SQL_TYPES: frozenset[str] = frozenset(
    {"integer", "int", "real", "float", "double", "numeric", "decimal",
     "text", "char", "varchar", "blob", "boolean", "timestamp"}
)

_LIMIT_TAIL_RE = re.compile(r"\bLIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE)
_CTE_NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s+AS\s*\(", re.IGNORECASE)
_QUOTES = "\"`[]"


class SafetyError(Exception):
    """Base class for safety-layer failures."""


class QueryExecutionError(SafetyError):
    """A validated query failed to execute (e.g. a SQL error from SQLite)."""


class QueryTimeoutError(QueryExecutionError):
    """A query exceeded the allowed execution time and was interrupted."""


# --------------------------------------------------------------------------- #
# Schema metadata                                                             #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _schema_metadata() -> tuple[frozenset[str], frozenset[str]]:
    """Return the set of table names and the global set of column names.

    Both sets are lowercased for case-insensitive comparison. Cached for the
    life of the process.

    Returns:
        A ``(table_names, all_column_names)`` tuple of frozensets.
    """
    inspector = db.get_inspector()
    table_names: set[str] = set()
    all_columns: set[str] = set()
    for table in inspector.get_table_names():
        table_names.add(table.lower())
        for column in inspector.get_columns(table):
            all_columns.add(column["name"].lower())
    return frozenset(table_names), frozenset(all_columns)


# --------------------------------------------------------------------------- #
# Token helpers                                                               #
# --------------------------------------------------------------------------- #
def _is_keyword(token: sqlparse.sql.Token) -> bool:
    """Return True if the token is any kind of SQL keyword."""
    return token.ttype is not None and str(token.ttype).startswith("Token.Keyword")


def _is_name(token: sqlparse.sql.Token) -> bool:
    """Return True if the token is an identifier name."""
    return token.ttype is not None and str(token.ttype).startswith("Token.Name")


def _significant_leaves(parsed: sqlparse.sql.Statement) -> list[sqlparse.sql.Token]:
    """Flatten a parsed statement to leaf tokens, dropping whitespace/comments.

    Args:
        parsed: A parsed sqlparse statement.

    Returns:
        The list of meaningful leaf tokens in document order.
    """
    leaves = []
    for token in parsed.flatten():
        if token.is_whitespace:
            continue
        if token.ttype is not None and str(token.ttype).startswith("Token.Comment"):
            continue
        leaves.append(token)
    return leaves


def _clean(identifier: str) -> str:
    """Strip quoting characters and lowercase an identifier."""
    return identifier.strip(_QUOTES).lower()


def _collect_aliases(token: sqlparse.sql.Token, out: set[str]) -> None:
    """Recursively collect identifier aliases (``x AS y`` and ``x y``).

    Args:
        token: The token (or group) to walk.
        out: Set accumulating lowercased alias names.
    """
    if isinstance(token, Identifier):
        alias = token.get_alias()
        if alias:
            out.add(_clean(alias))
    if token.is_group:
        for child in token.tokens:
            _collect_aliases(child, out)


def _extract_table_refs(leaves: list[sqlparse.sql.Token]) -> set[str]:
    """Extract base-table references (names following FROM/JOIN/`,`).

    Subqueries (a ``(`` after FROM) and the names that follow them are skipped,
    so only real base-table or CTE references are returned.

    Args:
        leaves: Significant leaf tokens of the statement.

    Returns:
        A set of lowercased table reference names.
    """
    refs: set[str] = set()
    in_from = False
    expect_table = False
    for token in leaves:
        upper = token.value.upper()
        if _is_keyword(token) and upper == "FROM":
            in_from, expect_table = True, True
            continue
        if _is_keyword(token) and upper.endswith("JOIN"):
            in_from, expect_table = False, True
            continue
        if _is_keyword(token) and upper.split()[0] in _FROM_BOUNDARY:
            in_from, expect_table = False, False
            continue
        if expect_table:
            if token.ttype is Punctuation and token.value == "(":
                expect_table = False  # derived table / subquery
                continue
            if _is_name(token):
                refs.add(_clean(token.value))
                expect_table = False
                continue
            expect_table = False
            continue
        if in_from and token.ttype is Punctuation and token.value == ",":
            expect_table = True
    return refs


def _extract_qualified_columns(
    leaves: list[sqlparse.sql.Token],
) -> tuple[list[str], set[int]]:
    """Extract column names from qualified references like ``alias.column``.

    Args:
        leaves: Significant leaf tokens of the statement.

    Returns:
        A tuple of (column names referenced after a dot, set of leaf indices
        that participate in a qualified reference so the bare-column scan can
        skip them). ``*`` wildcards are ignored.
    """
    columns: list[str] = []
    used_indices: set[int] = set()
    for i, token in enumerate(leaves):
        if token.ttype is Punctuation and token.value == "." and 0 < i < len(leaves) - 1:
            qualifier, column = leaves[i - 1], leaves[i + 1]
            if _is_name(qualifier) and (_is_name(column) or column.ttype is Wildcard):
                used_indices.update({i - 1, i, i + 1})
                if column.ttype is not Wildcard:
                    columns.append(_clean(column.value))
    return columns, used_indices


def _extract_bare_columns(
    leaves: list[sqlparse.sql.Token],
    qualified_indices: set[int],
    known_names: frozenset[str],
) -> set[str]:
    """Find unqualified name tokens that are not any known identifier.

    A name is reported only if it is not a known column, table, alias, CTE, or
    function — i.e. a probable hallucinated column. Function calls (a name
    immediately followed by ``(``) are skipped.

    Args:
        leaves: Significant leaf tokens of the statement.
        qualified_indices: Leaf indices already consumed by qualified refs.
        known_names: Union of all valid/derived identifier names (lowercased).

    Returns:
        Set of lowercased unknown bare column names.
    """
    unknown: set[str] = set()
    for i, token in enumerate(leaves):
        if i in qualified_indices or not _is_name(token):
            continue
        nxt = leaves[i + 1] if i + 1 < len(leaves) else None
        if nxt is not None and nxt.ttype is Punctuation and nxt.value == "(":
            continue  # function call
        name = _clean(token.value)
        if name not in known_names:
            unknown.add(name)
    return unknown


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #
def validate_sql(sql: str) -> tuple[bool, str]:
    """Validate that a SQL string is a safe, read-only, schema-valid query.

    Checks, in order: non-empty; exactly one statement; begins with SELECT or
    WITH; contains no forbidden (write) keywords; every referenced table
    exists; every qualified or bare column resolves to a real column (or a
    derived alias/CTE name).

    Args:
        sql: The SQL statement to validate.

    Returns:
        A ``(is_valid, message)`` tuple. When invalid, the message explains
        precisely why so the UI can show it to the user.
    """
    if not sql or not sql.strip():
        return False, "Query is empty."

    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) > 1:
        return False, "Only a single SQL statement is allowed (found multiple)."

    parsed = sqlparse.parse(statements[0])[0]
    leaves = _significant_leaves(parsed)
    keyword_values = [t.value.upper() for t in leaves if _is_keyword(t)]

    first_keyword = next((kw for kw in keyword_values), None)
    if first_keyword not in {"SELECT", "WITH"}:
        return False, "Only read-only SELECT (or WITH ... SELECT) queries are allowed."

    forbidden = FORBIDDEN_KEYWORDS.intersection(keyword_values)
    if forbidden:
        return False, f"Forbidden operation(s): {', '.join(sorted(forbidden))}."

    if "SELECT" not in keyword_values:
        return False, "Query must contain a SELECT."

    table_names, all_columns = _schema_metadata()

    # Names introduced by the query itself: CTE names and column/table aliases.
    # These are legitimate references even though they are not in the schema.
    cte_names = {_clean(m.group(1)) for m in _CTE_NAME_RE.finditer(statements[0])}
    aliases: set[str] = set()
    _collect_aliases(parsed, aliases)

    # Tables: CTE names are not real tables and are excluded from the check.
    referenced_tables = _extract_table_refs(leaves) - cte_names
    unknown_tables = referenced_tables - table_names
    if unknown_tables:
        return False, f"Unknown table(s): {', '.join(sorted(unknown_tables))}."

    # Columns. A reference is valid if it is a real column or a derived
    # alias introduced elsewhere in the query (e.g. a subquery/CTE output).
    column_allowed = all_columns | aliases
    qualified_columns, qualified_indices = _extract_qualified_columns(leaves)
    unknown_qualified = {c for c in qualified_columns if c not in column_allowed}
    if unknown_qualified:
        return False, f"Unknown column(s): {', '.join(sorted(unknown_qualified))}."

    known_names = column_allowed | table_names | cte_names | _SQL_FUNCTIONS | _SQL_TYPES
    unknown_bare = _extract_bare_columns(leaves, qualified_indices, known_names)
    if unknown_bare:
        return False, f"Unknown column(s): {', '.join(sorted(unknown_bare))}."

    return True, "Read-only query validated."


def ensure_limit(sql: str, row_limit: int = config.SQL_ROW_LIMIT) -> str:
    """Append a ``LIMIT`` clause if the query has no top-level limit.

    Args:
        sql: The SQL statement (assumed already validated).
        row_limit: Maximum number of rows to return.

    Returns:
        The SQL with a guaranteed top-level ``LIMIT`` clause.
    """
    parsed = sqlparse.parse(sql)[0]
    has_top_level_limit = any(
        _is_keyword(tok) and tok.value.upper() == "LIMIT" for tok in parsed.tokens
    )
    stripped = sql.rstrip().rstrip(";").rstrip()
    if has_top_level_limit or _LIMIT_TAIL_RE.search(stripped):
        return stripped
    return f"{stripped} LIMIT {row_limit}"


def safe_execute(
    sql: str,
    timeout: int = config.SQL_TIMEOUT_S,
    row_limit: int = config.SQL_ROW_LIMIT,
) -> pd.DataFrame:
    """Execute a read-only query with a timeout and an injected row limit.

    A watchdog thread interrupts the underlying SQLite connection if the query
    runs longer than ``timeout`` seconds. A ``LIMIT`` is injected when the query
    lacks one. The query is run on the read-only engine from :mod:`src.db`.

    Args:
        sql: A validated, read-only SQL statement.
        timeout: Maximum wall-clock execution time in seconds.
        row_limit: Maximum number of rows to return (injected if absent).

    Returns:
        The query result as a pandas DataFrame.

    Raises:
        QueryTimeoutError: If the query exceeds ``timeout`` seconds.
        QueryExecutionError: If SQLite reports any other execution error.
    """
    limited_sql = ensure_limit(sql, row_limit)
    connection = db.get_engine().connect()
    dbapi_connection = connection.connection.driver_connection

    timed_out = threading.Event()

    def _interrupt() -> None:
        timed_out.set()
        dbapi_connection.interrupt()

    watchdog = threading.Timer(timeout, _interrupt)
    try:
        watchdog.start()
        # Execute through SQLAlchemy (not pandas.read_sql) so SQLite errors
        # surface as sa.exc.OperationalError rather than being re-wrapped,
        # keeping timeout-vs-error detection reliable.
        result = connection.execute(sa.text(limited_sql))
        return pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    except sa.exc.OperationalError as exc:
        if timed_out.is_set():
            raise QueryTimeoutError(
                f"Query exceeded the {timeout}s time limit and was cancelled."
            ) from exc
        raise QueryExecutionError(str(exc.orig).strip()) from exc
    except sa.exc.SQLAlchemyError as exc:
        raise QueryExecutionError(str(getattr(exc, "orig", exc)).strip()) from exc
    finally:
        watchdog.cancel()
        connection.close()
