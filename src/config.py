"""Central configuration for Talk-to-Your-Data.

Every tunable value the application relies on — filesystem paths, database
connection settings, LLM parameters, SQL safety limits, and the token cost
model — lives here. Business logic imports from this module and never hardcodes
these literals, so behaviour can be changed in exactly one place.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Filesystem paths                                                            #
# All paths are derived from the project root via pathlib; never use raw       #
# string paths elsewhere in the codebase.                                      #
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

DATA_DIR: Path = PROJECT_ROOT / "data"
RAW_DATA_DIR: Path = DATA_DIR / "raw"
DB_PATH: Path = DATA_DIR / "olist.db"

LOGS_DIR: Path = PROJECT_ROOT / "logs"
QUERY_LOG_PATH: Path = LOGS_DIR / "queries.jsonl"

SRC_DIR: Path = PROJECT_ROOT / "src"
PROMPTS_DIR: Path = SRC_DIR / "prompts"
NL2SQL_PROMPT_PATH: Path = PROMPTS_DIR / "nl2sql_system.md"
INTERPRET_PROMPT_PATH: Path = PROMPTS_DIR / "interpret_system.md"

DOCS_DIR: Path = PROJECT_ROOT / "docs"
SCHEMA_DOC_PATH: Path = DOCS_DIR / "schema.md"

EVAL_DIR: Path = PROJECT_ROOT / "eval"
EVAL_RESULTS_DIR: Path = EVAL_DIR / "results"
TEST_QUESTIONS_PATH: Path = EVAL_DIR / "test_questions.json"

# --------------------------------------------------------------------------- #
# Database                                                                    #
# --------------------------------------------------------------------------- #
DB_URL: str = f"sqlite:///{DB_PATH}"

# Rows per INSERT batch when loading CSVs. Keeps peak memory flat on the
# ~1M-row geolocation table while staying well clear of SQLite's bound
# parameter ceiling.
DB_LOAD_CHUNKSIZE: int = 10_000

# --------------------------------------------------------------------------- #
# LLM — Google Gemini                                                         #
# --------------------------------------------------------------------------- #
# Load .env from the project root so GEMINI_API_KEY is available on import.
load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
# gemini-2.5-flash is the chosen model — a workable balance of capability and
# free-tier quota. (gemini-2.0-flash's free quota is now 0; gemini-3.5-flash is
# limited to 5 requests/minute and ~20/day, too tight for batch evaluation or an
# interactive demo.) The free tier caps requests per day, so the eval harness
# runs a 15-question subset (`--subset`); see eval/run_eval.py and the README.
GEMINI_MODEL: str = "gemini-2.5-flash"
GEMINI_TEMPERATURE: float = 0.0          # deterministic NL->SQL
GEMINI_MAX_OUTPUT_TOKENS: int = 2048
GEMINI_REQUEST_TIMEOUT_S: int = 30

# Retry policy for transient API failures (consumed by the tenacity decorator in
# src/nl2sql.py). Limited to a single retry on purpose: on a free tier a 429 is
# usually a standing rate/quota limit that will not clear in a few seconds, and
# every rejected attempt still counts against the daily request quota — so
# retrying hard burns the daily budget instead of recovering. One short backoff
# covers genuinely transient blips without amplifying quota usage on failures.
LLM_RETRY_ATTEMPTS: int = 2
LLM_RETRY_MIN_WAIT_S: float = 4.0
LLM_RETRY_MAX_WAIT_S: float = 30.0

# --------------------------------------------------------------------------- #
# SQL safety / execution limits                                               #
# --------------------------------------------------------------------------- #
SQL_ROW_LIMIT: int = 1_000        # auto-injected LIMIT when a query omits one
SQL_TIMEOUT_S: int = 10           # hard cap on query execution time

# --------------------------------------------------------------------------- #
# Token cost model — Gemini 2.5 Flash                                         #
# The free tier costs $0 in practice; these are Google's published paid-tier   #
# rates (USD per 1,000,000 tokens) so the UI can surface a *theoretical* cost  #
# per query. Update these if the model changes. See README "Production         #
# considerations".                                                            #
# --------------------------------------------------------------------------- #
GEMINI_INPUT_COST_PER_1M_USD: float = 0.30
GEMINI_OUTPUT_COST_PER_1M_USD: float = 2.50

# Pause between questions in the eval harness to respect free-tier rate limits.
EVAL_SLEEP_BETWEEN_QUESTIONS_S: int = 5


def require_gemini_api_key() -> str:
    """Return the configured Gemini API key, or raise if it is missing.

    Returns:
        The API key read from the ``GEMINI_API_KEY`` environment variable.

    Raises:
        RuntimeError: If the key is not set, with guidance on how to fix it.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your "
            "key (free at https://aistudio.google.com/app/apikey)."
        )
    return GEMINI_API_KEY


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimate the theoretical USD cost of one Gemini request.

    Uses the configured model's published paid-tier rates. The free tier bills
    nothing; this figure exists for monitoring and reporting purposes.

    Args:
        input_tokens: Number of prompt (input) tokens billed.
        output_tokens: Number of completion (output) tokens billed.

    Returns:
        Estimated cost in US dollars.
    """
    return (
        input_tokens / 1_000_000 * GEMINI_INPUT_COST_PER_1M_USD
        + output_tokens / 1_000_000 * GEMINI_OUTPUT_COST_PER_1M_USD
    )
