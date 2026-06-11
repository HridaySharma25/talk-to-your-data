"""Natural-language-to-SQL translation via Google Gemini.

:func:`question_to_sql` sends a business question, the schema, and optional
conversation history to ``gemini-2.0-flash`` and returns the generated SQL
together with the model's reasoning and request metrics (tokens, latency).

Design notes:
* The model is asked for a strict JSON object (``{"reasoning", "sql"}``) via
  JSON response mode, so output is structured rather than free text.
* ``temperature=0`` for deterministic translation.
* The API call is wrapped in a tenacity retry with exponential backoff to ride
  out free-tier rate limits and transient 5xx errors.
* If the question cannot be answered from the schema, the model returns
  ``"NULL"`` and we surface ``sql=None``.
"""

from __future__ import annotations

import json
import re
import time
import warnings
from functools import lru_cache

from google.api_core import exceptions as google_exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# The google.generativeai package prints a deprecation FutureWarning on import;
# silence it so it does not pollute the CLI/Streamlit output.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import google.generativeai as genai

from src import config

# Transient errors worth retrying (rate limits, timeouts, transient 5xx).
_TRANSIENT_ERRORS = (
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
)

_SCHEMA_PLACEHOLDER = "{{SCHEMA}}"
_CODE_FENCE_RE = re.compile(r"^\s*```(?:sql|json)?\s*|\s*```\s*$", re.IGNORECASE)
_NULL_TOKENS = {"", "NULL", "NONE"}

_configured = False


class Nl2SqlError(Exception):
    """Raised when the model request fails (after retries) or returns no usable output."""


@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    """Load and cache the NL->SQL system prompt template.

    Returns:
        The raw template text, including the ``{{SCHEMA}}`` placeholder.
    """
    return config.NL2SQL_PROMPT_PATH.read_text(encoding="utf-8")


def _ensure_configured() -> None:
    """Configure the Gemini client with the API key exactly once."""
    global _configured
    if not _configured:
        genai.configure(api_key=config.require_gemini_api_key())
        _configured = True


def _strip_code_fences(text: str) -> str:
    """Remove surrounding markdown code fences from a string.

    Args:
        text: Possibly fenced text (e.g. ```sql ... ```).

    Returns:
        The text with leading/trailing fences and whitespace removed.
    """
    cleaned = _CODE_FENCE_RE.sub("", text.strip())
    return cleaned.strip().strip("`").strip()


def _build_model(schema: str) -> genai.GenerativeModel:
    """Construct the Gemini model with the schema-injected system instruction.

    Args:
        schema: The Markdown schema description to embed in the prompt.

    Returns:
        A configured :class:`google.generativeai.GenerativeModel`.
    """
    system_instruction = _load_prompt_template().replace(_SCHEMA_PLACEHOLDER, schema)
    return genai.GenerativeModel(
        model_name=config.GEMINI_MODEL,
        system_instruction=system_instruction,
        generation_config=genai.GenerationConfig(
            temperature=config.GEMINI_TEMPERATURE,
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
        ),
    )


def _build_contents(question: str, history: list[dict] | None) -> list[dict]:
    """Assemble the multi-turn content list for the request.

    Args:
        question: The current natural-language question.
        history: Optional prior turns, each a dict with ``question`` and
            ``sql`` keys, supplied for follow-up context.

    Returns:
        A list of Gemini ``content`` dicts ending with the current question.
    """
    contents: list[dict] = []
    for turn in history or []:
        prior_sql = turn.get("sql") or "NULL"
        contents.append({"role": "user", "parts": [turn.get("question", "")]})
        contents.append(
            {"role": "model", "parts": [json.dumps({"reasoning": "", "sql": prior_sql})]}
        )
    contents.append({"role": "user", "parts": [question]})
    return contents


@retry(
    reraise=True,
    stop=stop_after_attempt(config.LLM_RETRY_ATTEMPTS),
    wait=wait_exponential(
        multiplier=config.LLM_RETRY_MIN_WAIT_S, max=config.LLM_RETRY_MAX_WAIT_S
    ),
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
)
def _generate(model: genai.GenerativeModel, contents: list[dict]):
    """Call the Gemini API, retrying transient failures with backoff."""
    return model.generate_content(contents)


def _parse_response(raw_text: str) -> tuple[str | None, str]:
    """Parse the model's JSON response into (sql, reasoning).

    Falls back to treating the whole payload as SQL if it is not valid JSON.
    Normalizes the documented "cannot answer" sentinel to ``sql=None``.

    Args:
        raw_text: The raw text returned by the model.

    Returns:
        A ``(sql, reasoning)`` tuple; ``sql`` is ``None`` when unanswerable.
    """
    sql: str | None
    reasoning = ""
    try:
        data = json.loads(raw_text)
        sql = data.get("sql")
        reasoning = (data.get("reasoning") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        sql = _strip_code_fences(raw_text)

    if sql is not None:
        sql = _strip_code_fences(str(sql))
        if sql.upper() in _NULL_TOKENS:
            sql = None
    return sql, reasoning


def question_to_sql(
    question: str,
    schema: str,
    history: list[dict] | None = None,
) -> dict:
    """Translate a natural-language question into SQL using Gemini.

    Args:
        question: The business user's question.
        schema: The Markdown schema description (from ``schema.get_schema_for_llm``).
        history: Optional list of prior ``{"question", "sql"}`` turns for context.

    Returns:
        A dict with keys:
            ``sql`` (str | None — ``None`` if the question is unanswerable),
            ``reasoning`` (str), ``tokens_in`` (int), ``tokens_out`` (int),
            ``latency_ms`` (int), ``model`` (str), ``estimated_cost_usd`` (float).

    Raises:
        Nl2SqlError: If the request fails after retries or yields no output.
    """
    _ensure_configured()
    model = _build_model(schema)
    contents = _build_contents(question, history)

    start = time.perf_counter()
    try:
        response = _generate(model, contents)
        raw_text = response.text
    except Exception as exc:  # API boundary: normalize any failure to a domain error
        raise Nl2SqlError(f"Gemini request failed: {exc}") from exc
    latency_ms = int((time.perf_counter() - start) * 1000)

    sql, reasoning = _parse_response(raw_text)

    usage = getattr(response, "usage_metadata", None)
    tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
    tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)

    return {
        "sql": sql,
        "reasoning": reasoning,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "model": config.GEMINI_MODEL,
        "estimated_cost_usd": config.estimate_cost_usd(tokens_in, tokens_out),
    }
