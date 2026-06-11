"""Talk to Your Data — Streamlit front end.

Wires the full pipeline together: a natural-language question is turned into
SQL (Gemini), validated by the safety layer, executed against SQLite,
visualized with Plotly, and summarized back into plain English — with every
request logged. The UI exposes clickable example questions, a staged progress
indicator, the generated SQL, the chart, an executive summary, the raw data,
a latency/token/cost footer, and a query-history panel.

Run with:
    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import html
import sys
import time
from pathlib import Path

# Make the `src` package importable when Streamlit runs this file directly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from src import config, interpret, logger, nl2sql, safety, schema, visualize

EXAMPLE_QUESTIONS: list[str] = [
    "What were the top 5 product categories by revenue in 2018?",
    "How many orders were placed each month in 2018?",
    "What is the average review score for the 10 best-selling product categories?",
    "Which 10 customer states generate the most revenue?",
    "What share of orders used each payment type?",
    "What is the average delivery time in days by customer state?",
]

MAX_HISTORY = 10


@st.cache_resource(show_spinner=False)
def _load_schema() -> str:
    """Load and cache the LLM schema description for the app session."""
    return schema.get_schema_for_llm()


def _inject_css() -> None:
    """Inject a small amount of CSS for the summary callout box."""
    st.markdown(
        """
        <style>
        .summary-callout {
            background: #eef6ff;
            border-left: 5px solid #1c7ed6;
            padding: 1rem 1.25rem;
            border-radius: 6px;
            font-size: 1.03rem;
            line-height: 1.5;
            margin: 0.5rem 0 1rem 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _use_question(question: str) -> None:
    """Callback: populate the input with a question and trigger a run."""
    st.session_state["question"] = question
    st.session_state["trigger"] = True


def render_sidebar() -> None:
    """Render the example-questions and query-history panels."""
    with st.sidebar:
        st.header("💡 Example questions")
        for i, example in enumerate(EXAMPLE_QUESTIONS):
            st.button(
                example,
                key=f"example_{i}",
                on_click=_use_question,
                args=(example,),
                width="stretch",
            )

        st.divider()
        st.header("🕑 Query history")
        history = st.session_state.get("history", [])
        if not history:
            st.caption("Your last 10 questions will appear here.")
        for i, item in enumerate(reversed(history[-MAX_HISTORY:])):
            marker = "✅" if item["success"] else "⚠️"
            st.button(
                f"{marker} {item['question']}",
                key=f"history_{i}",
                on_click=_use_question,
                args=(item["question"],),
                width="stretch",
            )


def process_question(question: str, schema_doc: str) -> dict:
    """Run the full NL->SQL->execute->chart->summarize pipeline.

    Renders a staged status indicator while running. All stage failures are
    captured into the returned outcome rather than raised.

    Args:
        question: The user's natural-language question.
        schema_doc: The schema description passed to the model.

    Returns:
        An outcome dict with a ``status`` key (one of ``ok``, ``unanswerable``,
        ``gen_error``, ``validation_failed``, ``exec_error``) plus the artifacts
        produced (sql, df, fig, summary, metrics) where applicable.
    """
    outcome: dict = {"question": question}
    start = time.perf_counter()

    with st.status("Working on it…", expanded=True) as status:
        st.write("🧠 Generating SQL…")
        try:
            generation = nl2sql.question_to_sql(question, schema_doc)
        except nl2sql.Nl2SqlError as exc:
            status.update(label="Couldn't generate SQL", state="error", expanded=False)
            return {**outcome, "status": "gen_error", "error": str(exc)}

        outcome["generation"] = generation
        sql = generation["sql"]
        outcome["sql"] = sql

        if sql is None:
            status.update(label="Not answerable from this data", state="complete", expanded=False)
            return {**outcome, "status": "unanswerable", "reasoning": generation["reasoning"]}

        st.write("🛡️ Validating…")
        is_valid, message = safety.validate_sql(sql)
        outcome["validation"] = (is_valid, message)
        if not is_valid:
            status.update(label="SQL failed validation", state="error", expanded=False)
            return {**outcome, "status": "validation_failed", "error": message}

        st.write("🗄️ Querying the database…")
        try:
            exec_start = time.perf_counter()
            df = safety.safe_execute(sql)
            outcome["exec_ms"] = int((time.perf_counter() - exec_start) * 1000)
        except safety.QueryExecutionError as exc:
            status.update(label="Query failed to run", state="error", expanded=False)
            return {**outcome, "status": "exec_error", "error": str(exc)}
        outcome["df"] = df

        st.write("📊 Building the visualization…")
        outcome["fig"] = visualize.auto_chart(df, question)

        st.write("📝 Summarizing…")
        try:
            interp_start = time.perf_counter()
            outcome["summary"] = interpret.summarize_result(question, sql, df)
            outcome["interp_ms"] = int((time.perf_counter() - interp_start) * 1000)
        except interpret.InterpretationError as exc:
            outcome["summary"] = None
            outcome["summary_error"] = str(exc)
            outcome["interp_ms"] = None

        status.update(label="Done", state="complete", expanded=False)

    outcome["status"] = "ok"
    outcome["total_ms"] = int((time.perf_counter() - start) * 1000)
    return outcome


def _render_footer(outcome: dict) -> None:
    """Render the latency / token / cost footer for a successful query."""
    generation = outcome["generation"]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total latency", f"{outcome['total_ms']:,} ms")
    col2.metric("Rows returned", f"{len(outcome['df']):,}")
    col3.metric("Tokens (in / out)", f"{generation['tokens_in']} / {generation['tokens_out']}")
    col4.metric("Est. cost", f"${generation['estimated_cost_usd']:.6f}")
    interp_ms = outcome.get("interp_ms")
    interp_label = f"{interp_ms} ms" if interp_ms is not None else "n/a"
    st.caption(
        f"⏱ Breakdown — SQL generation {generation['latency_ms']} ms · "
        f"query {outcome.get('exec_ms', '?')} ms · summary {interp_label} · "
        f"model `{generation['model']}` · cost is theoretical (Gemini free tier bills $0)."
    )


def render_outcome(outcome: dict) -> None:
    """Render the result (or a helpful error) for a processed question."""
    status = outcome["status"]

    if status == "gen_error":
        st.error(f"I couldn't generate SQL for that question.\n\n**Details:** {outcome['error']}")
        st.info(
            "Try rephrasing, or ask about the e-commerce data: orders, products, "
            "customers, payments, reviews, or sellers."
        )
        return

    if status == "unanswerable":
        st.warning("I can't answer that with the available data.")
        if outcome.get("reasoning"):
            st.caption(outcome["reasoning"])
        return

    if status == "validation_failed":
        st.error(f"The generated SQL did not pass the safety check: **{outcome['error']}**")
        with st.expander("Show the rejected SQL", expanded=True):
            st.code(outcome.get("sql") or "", language="sql")
        return

    if status == "exec_error":
        st.error(f"The query failed to run: {outcome['error']}")
        with st.expander("Show the SQL", expanded=True):
            st.code(outcome.get("sql") or "", language="sql")
        return

    # status == "ok"
    df = outcome["df"]
    with st.expander("🔎 Generated SQL", expanded=False):
        st.code(outcome["sql"], language="sql")

    st.plotly_chart(outcome["fig"], width="stretch")

    if outcome.get("summary"):
        st.markdown(
            f'<div class="summary-callout">💡 {html.escape(outcome["summary"])}</div>',
            unsafe_allow_html=True,
        )
    elif outcome.get("summary_error"):
        st.warning(f"Summary unavailable: {outcome['summary_error']}")

    with st.expander(f"📄 Raw data ({len(df):,} rows)", expanded=False):
        st.dataframe(df, width="stretch", hide_index=True)
        st.download_button(
            "Download CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name="result.csv",
            mime="text/csv",
        )

    _render_footer(outcome)


def _record_history(outcome: dict) -> None:
    """Append a processed question to the session history."""
    st.session_state.setdefault("history", []).append(
        {
            "question": outcome["question"],
            "success": outcome["status"] in ("ok", "unanswerable"),
        }
    )


def _log_outcome(outcome: dict) -> None:
    """Persist a processed question to the JSONL query log."""
    generation = outcome.get("generation") or {}
    validation = outcome.get("validation") or (None, None)
    record = logger.QueryLogRecord(
        question=outcome["question"],
        generated_sql=outcome.get("sql"),
        validation_passed=validation[0],
        validation_message=validation[1],
        row_count=len(outcome["df"]) if "df" in outcome else None,
        latency_ms=outcome.get("total_ms") or generation.get("latency_ms"),
        input_tokens=generation.get("tokens_in"),
        output_tokens=generation.get("tokens_out"),
        estimated_cost_usd=generation.get("estimated_cost_usd"),
        success=outcome["status"] in ("ok", "unanswerable"),
        error_message=outcome.get("error") or outcome.get("summary_error"),
    )
    logger.log_query(record)


def main() -> None:
    """Application entry point."""
    st.set_page_config(page_title="Talk to Your Data", page_icon="📊", layout="wide")
    _inject_css()

    st.title("📊 Talk to Your Data")
    st.caption(
        "Ask questions in plain English about the Olist Brazilian e-commerce dataset. "
        "Your question is translated to SQL, validated, executed, charted, and summarized."
    )

    if not config.DB_PATH.exists():
        st.error(
            f"Database not found at `{config.DB_PATH}`. "
            "Build it first with `python scripts/01_build_db.py`."
        )
        st.stop()
    if not config.GEMINI_API_KEY:
        st.error("`GEMINI_API_KEY` is not set. Add it to your `.env` file and reload.")
        st.stop()

    schema_doc = _load_schema()
    st.session_state.setdefault("history", [])

    st.text_input(
        "Your question",
        key="question",
        placeholder="e.g. What were the top 5 product categories by revenue in 2018?",
    )
    ask = st.button("Ask", type="primary")
    trigger = st.session_state.pop("trigger", False)

    outcome: dict | None = None
    question = st.session_state.get("question", "").strip()
    if (ask or trigger) and question:
        outcome = process_question(question, schema_doc)
        _record_history(outcome)
        _log_outcome(outcome)

    render_sidebar()

    if outcome is not None:
        render_outcome(outcome)
    elif (ask or trigger) and not question:
        st.info("Please type a question first, or pick an example from the sidebar.")


main()
