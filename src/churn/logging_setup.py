"""Logging unificado para CLI, batch y API."""

from __future__ import annotations

import logging
import os

from rich.logging import RichHandler


def setup_logging(level: str | None = None) -> None:
    lvl = (level or os.getenv("CHURN_LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=lvl,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=False)],
        force=True,
    )
    logging.getLogger("py4j").setLevel(logging.WARNING)
