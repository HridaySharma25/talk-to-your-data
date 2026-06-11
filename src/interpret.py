"""Result interpretation via Google Gemini.

:func:`summarize_result` turns a query result into a short, plain-English
executive summary. It sends the model the original question, the SQL that ran,
and a compact, token-bounded representation of the DataFrame (shape, head, and
numeric/categorical summaries) rather than the raw data, then returns a 2-3
sentence summary suitable for a business stakeholder.
"""

from __future__ import annotations

import warnings
from functools import lru_cache

import pandas as pd
from google.api_core import exceptions as google_exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Silence the google.generativeai import-time deprecation FutureWarning.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai

from src import config

_TRANSIENT_ERRORS = (
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
)

_HEAD_ROWS = 10
_MAX_VALUE_COUNT_CARDINALITY = 20

_configured = False


class InterpretationError(Exception):
    """Raised when the summarization request fails after retries."""


@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    """Load and cache the interpretation system prompt."""
    return config.INTERPRET_PROMPT_PATH.read_text(encoding="utf-8")


def _ensure_configured() -> None:
    """Configure the Gemini client with the API key exactly once."""
    global _configured
    if not _configured:
        genai.configure(api_key=config.require_gemini_api_key())
        _configured = True


def _compact_representation(df: pd.DataFrame, head_rows: int = _HEAD_ROWS) -> str:
    """Build a compact, token-bounded text summary of a result DataFrame.

    Includes shape and dtypes, the first rows, and — for larger results —
    a numeric ``describe`` and value counts for low-cardinality categoricals.

    Args:
        df: The query result.
        head_rows: Number of leading rows to include verbatim.

    Returns:
        A plain-text representation safe to embed in a prompt.
    """
    if df.empty:
        return f"The query returned 0 rows. Columns: {', '.join(df.columns) or '(none)'}."

    parts = [
        f"Shape: {len(df)} rows x {df.shape[1]} columns.",
        "Columns: " + ", ".join(f"{c} ({df[c].dtype})" for c in df.columns),
        f"First {min(head_rows, len(df))} row(s):",
        df.head(head_rows).to_string(index=False),
    ]

    if len(df) > head_rows:
        numeric = df.select_dtypes(include="number")
        if not numeric.empty:
            parts.append("Numeric summary:")
            parts.append(numeric.describe().round(2).to_string())
        # Non-numeric, non-datetime columns (robust to pandas-3 `str` dtype).
        categorical = df.select_dtypes(exclude=["number", "datetime"])
        for column in categorical.columns:
            if df[column].nunique(dropna=True) <= _MAX_VALUE_COUNT_CARDINALITY:
                parts.append(f"Value counts for {column} (top 5):")
                parts.append(df[column].value_counts().head(5).to_string())

    return "\n".join(parts)


def _build_model() -> genai.GenerativeModel:
    """Construct the Gemini model used for summarization."""
    return genai.GenerativeModel(
        model_name=config.GEMINI_MODEL,
        system_instruction=_load_prompt_template(),
        generation_config=genai.GenerationConfig(
            temperature=config.GEMINI_TEMPERATURE,
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
        ),
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(config.LLM_RETRY_ATTEMPTS),
    wait=wait_exponential(
        multiplier=config.LLM_RETRY_MIN_WAIT_S, max=config.LLM_RETRY_MAX_WAIT_S
    ),
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
)
def _generate(model: genai.GenerativeModel, content: str):
    """Call the Gemini API, retrying transient failures with backoff."""
    return model.generate_content(content)


def summarize_result(question: str, sql: str, result_df: pd.DataFrame) -> str:
    """Produce a 2-3 sentence executive summary of a query result.

    Args:
        question: The original natural-language business question.
        sql: The SQL statement that produced ``result_df``.
        result_df: The query result.

    Returns:
        A plain-English summary string for a business stakeholder.

    Raises:
        InterpretationError: If the request fails after retries.
    """
    _ensure_configured()
    model = _build_model()
    content = (
        f"Business question:\n{question}\n\n"
        f"SQL executed:\n{sql}\n\n"
        f"Query result:\n{_compact_representation(result_df)}"
    )
    try:
        response = _generate(model, content)
        return response.text.strip()
    except Exception as exc:  # API boundary: normalize any failure to a domain error
        raise InterpretationError(f"Gemini request failed: {exc}") from exc
