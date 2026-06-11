"""Heuristic auto-charting with Plotly.

:func:`auto_chart` inspects the shape and dtypes of a result DataFrame and the
user's question, then picks a sensible chart:

* 1x1 numeric            -> KPI "big number" card
* datetime + numeric     -> line chart (time series)
* category + numeric     -> bar chart (<= a readable number of categories)
* two continuous numerics-> scatter plot
* one dimension + many   -> grouped bar / multi-line
  measures
* anything else          -> a data table fallback

All figures use the clean ``plotly_white`` theme, take their title from the
question, and use prettified axis labels.

Note: SQLite returns dates and ``strftime`` periods as text (e.g. ``'2017-07'``),
so temporal detection here is value- and name-aware, not just dtype-based.
"""

from __future__ import annotations

import re

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pandas.api import types as ptypes

# Presentation thresholds (chart-rendering policy, not business config).
MAX_BAR_CATEGORIES = 25       # above this a bar chart becomes unreadable -> table
DISCRETE_INT_MAX_CARDINALITY = 25  # low-cardinality ints are dimensions, not measures
TABLE_MAX_ROWS = 100          # cap rows rendered in a table figure (app shows full data)
_PLOTLY_TEMPLATE = "plotly_white"

_TEMPORAL_NAME_HINTS = ("date", "month", "year", "day", "week", "quarter", "period")
_TEMPORAL_VALUE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")  # YYYY / YYYY-MM / YYYY-MM-DD


def _prettify(name: str) -> str:
    """Turn a snake_case column name into a Title Case label."""
    return name.replace("_", " ").strip().title()


def _title_from_question(question: str) -> str:
    """Derive a chart title from the user's question."""
    title = (question or "").strip()
    if not title:
        return "Result"
    return title[0].upper() + title[1:]


def _is_numeric(series: pd.Series) -> bool:
    """True for numeric (non-boolean) columns."""
    return ptypes.is_numeric_dtype(series) and not ptypes.is_bool_dtype(series)


def _is_temporal(series: pd.Series, name: str) -> bool:
    """Decide whether a column represents a time axis.

    Handles real datetime dtypes, ``strftime`` text periods (``'2017-07'``),
    and year-like integer columns named accordingly.

    Args:
        series: The column values.
        name: The column name (used for hint words like "month"/"year").

    Returns:
        True if the column should be treated as a temporal axis.
    """
    if ptypes.is_datetime64_any_dtype(series):
        return True
    sample = series.dropna()
    if sample.empty:
        return False
    if not _is_numeric(series):  # string/object/category (incl. pandas-3 `str` dtype)
        as_str = sample.astype(str).head(25)
        if all(_TEMPORAL_VALUE_RE.match(value) for value in as_str):
            return True
    name_lower = name.lower()
    if any(hint in name_lower for hint in _TEMPORAL_NAME_HINTS) and _is_numeric(series):
        return True  # e.g. an integer "year" column
    return False


def _looks_discrete_dimension(series: pd.Series) -> bool:
    """True for integer-like columns with few distinct values (a dimension)."""
    if not ptypes.is_integer_dtype(series):
        return False
    return series.nunique(dropna=True) <= DISCRETE_INT_MAX_CARDINALITY


def _coerce_temporal(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Convert a temporal text column to datetime when cleanly parseable."""
    if ptypes.is_datetime64_any_dtype(df[column]):
        return df
    try:
        df = df.copy()
        df[column] = pd.to_datetime(df[column], errors="raise")
    except (ValueError, TypeError):
        pass  # leave as sortable text (lexical order is chronological for YYYY-MM)
    return df


def _finalize(fig: go.Figure, x_label: str | None, y_label: str | None) -> go.Figure:
    """Apply the shared theme and axis labels to a figure."""
    fig.update_layout(template=_PLOTLY_TEMPLATE, margin=dict(t=70, l=60, r=30, b=50))
    if x_label is not None:
        fig.update_xaxes(title_text=x_label)
    if y_label is not None:
        fig.update_yaxes(title_text=y_label, tickformat=",")
    return fig


def _kpi_card(df: pd.DataFrame, title: str) -> go.Figure:
    """Render a single numeric value as a KPI big-number card."""
    value = float(df.iloc[0, 0])
    value_format = ",.0f" if value.is_integer() else ",.2f"
    fig = go.Figure(
        go.Indicator(
            mode="number",
            value=value,
            number={"valueformat": value_format},
            title={"text": title, "font": {"size": 18}},
        )
    )
    fig.update_layout(template=_PLOTLY_TEMPLATE, height=260)
    return fig


def _bar(df: pd.DataFrame, dimension: str, measure: str, title: str) -> go.Figure:
    """Render a bar chart, sorted for readability."""
    data = df[[dimension, measure]].copy()
    if _is_numeric(data[dimension]):
        data = data.sort_values(dimension)            # ordinal dimension: natural order
    else:
        data = data.sort_values(measure, ascending=False)  # categories: rank by value
    fig = px.bar(data, x=dimension, y=measure, title=title)
    return _finalize(fig, _prettify(dimension), _prettify(measure))


def _line(df: pd.DataFrame, dimension: str, measure: str, title: str) -> go.Figure:
    """Render a time-series line chart."""
    data = _coerce_temporal(df[[dimension, measure]].copy(), dimension).sort_values(dimension)
    fig = px.line(data, x=dimension, y=measure, markers=True, title=title)
    return _finalize(fig, _prettify(dimension), _prettify(measure))


def _scatter(df: pd.DataFrame, x: str, y: str, title: str) -> go.Figure:
    """Render a scatter plot of two continuous measures."""
    fig = px.scatter(df, x=x, y=y, title=title)
    return _finalize(fig, _prettify(x), _prettify(y))


def _table(df: pd.DataFrame, title: str) -> go.Figure:
    """Render a result set as a Plotly table (fallback)."""
    shown = df.head(TABLE_MAX_ROWS)
    if len(df) > TABLE_MAX_ROWS:
        title = f"{title}  (first {TABLE_MAX_ROWS} of {len(df):,} rows)"
    fig = go.Figure(
        go.Table(
            header=dict(
                values=[f"<b>{_prettify(c)}</b>" for c in shown.columns],
                fill_color="#f1f3f5",
                align="left",
            ),
            cells=dict(
                values=[shown[c].tolist() for c in shown.columns],
                align="left",
                fill_color="white",
            ),
        )
    )
    fig.update_layout(template=_PLOTLY_TEMPLATE, title=title, margin=dict(t=60, l=10, r=10, b=10))
    return fig


def _empty_figure(title: str) -> go.Figure:
    """Render a placeholder for an empty result set."""
    fig = go.Figure()
    fig.update_layout(
        template=_PLOTLY_TEMPLATE,
        title=title,
        annotations=[
            dict(
                text="No data to display for this query.",
                showarrow=False,
                font=dict(size=16, color="#868e96"),
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
            )
        ],
    )
    return fig


def _two_column_chart(
    df: pd.DataFrame, numeric_cols: list[str], title: str
) -> go.Figure:
    """Choose a chart for a two-column result."""
    col0, col1 = df.columns[0], df.columns[1]

    if len(numeric_cols) == 1:
        measure = numeric_cols[0]
        dimension = col1 if measure == col0 else col0
        if _is_temporal(df[dimension], dimension):
            return _line(df, dimension, measure, title)
        if df[dimension].nunique(dropna=True) <= MAX_BAR_CATEGORIES:
            return _bar(df, dimension, measure, title)
        return _table(df, title)

    if len(numeric_cols) == 2:
        if _is_temporal(df[col0], col0):
            return _line(df, col0, col1, title)
        if _looks_discrete_dimension(df[col0]):
            return _bar(df, col0, col1, title)
        return _scatter(df, col0, col1, title)

    return _table(df, title)  # two non-numeric columns


def _multi_column_chart(
    df: pd.DataFrame, numeric_cols: list[str], title: str
) -> go.Figure:
    """Choose a chart for a result with more than two columns."""
    dimensions = [c for c in df.columns if c not in numeric_cols]
    measures = numeric_cols

    # One dimension + one or more measures -> multi-line (if temporal) or grouped bar.
    if len(dimensions) == 1 and measures and len(df) <= MAX_BAR_CATEGORIES:
        dimension = dimensions[0]
        if _is_temporal(df[dimension], dimension):
            data = _coerce_temporal(df.copy(), dimension).sort_values(dimension)
            fig = px.line(data, x=dimension, y=measures, markers=True, title=title)
        else:
            fig = px.bar(df, x=dimension, y=measures, barmode="group", title=title)
        fig = _finalize(fig, _prettify(dimension), "Value")
        fig.update_layout(legend_title_text="")
        return fig

    # Two dimensions + one measure -> colored/grouped bar.
    if len(dimensions) == 2 and len(measures) == 1 and len(df) <= 100:
        fig = px.bar(
            df, x=dimensions[0], y=measures[0], color=dimensions[1],
            barmode="group", title=title,
        )
        return _finalize(fig, _prettify(dimensions[0]), _prettify(measures[0]))

    return _table(df, title)


def auto_chart(df: pd.DataFrame, question: str) -> go.Figure:
    """Pick and build an appropriate Plotly figure for a query result.

    Args:
        df: The query result.
        question: The natural-language question (used for the chart title).

    Returns:
        A Plotly :class:`~plotly.graph_objects.Figure`. Always returns a figure
        (a placeholder for empty input), never raises on shape.
    """
    title = _title_from_question(question)
    if df is None or df.empty:
        return _empty_figure(title)

    numeric_cols = [c for c in df.columns if _is_numeric(df[c])]
    n_rows, n_cols = df.shape

    if n_rows == 1 and n_cols == 1 and len(numeric_cols) == 1:
        return _kpi_card(df, title)
    if n_cols == 1:
        return _table(df, title)
    if n_cols == 2:
        return _two_column_chart(df, numeric_cols, title)
    return _multi_column_chart(df, numeric_cols, title)
