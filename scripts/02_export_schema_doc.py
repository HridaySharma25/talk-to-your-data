"""Export the LLM schema description to a Markdown file.

Renders the same schema document the model sees at runtime and writes it to
``docs/schema.md`` so it can be reviewed, diffed, and linked from the README.

Run from the project root:

    python scripts/02_export_schema_doc.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import config, schema  # noqa: E402  (import after sys.path manipulation)

logger = logging.getLogger("export_schema_doc")


def main() -> None:
    """Render the schema document and write it to ``docs/schema.md``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    if not config.DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {config.DB_PATH}. Run scripts/01_build_db.py first."
        )

    document = schema.get_schema_for_llm()
    config.SCHEMA_DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.SCHEMA_DOC_PATH.write_text(document, encoding="utf-8")

    logger.info(
        "Wrote schema doc (%d chars, ~%d tokens) to %s",
        len(document),
        len(document) // 4,  # rough 4 chars/token heuristic
        config.SCHEMA_DOC_PATH,
    )


if __name__ == "__main__":
    main()
