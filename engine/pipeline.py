"""Single post-import pipeline shared by Watchdog and API callers."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError

from engine.obsidian_import import ImportStats, import_file
from engine.vector_store import DEFAULT_VECTOR_DB, update_vector

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineStats:
    read_markdown: int
    gemini_calls: int
    processed: int
    skipped: int
    failed: int
    vectors_updated: int = 0
    imported: int = 0
    vector_status: str = "disabled"


def _rag_enabled() -> bool:
    return os.getenv("ENABLE_LOCAL_RAG", "false").strip().casefold() == "true"


def _is_vector_unavailable(exc: Exception) -> bool:
    return isinstance(
        exc, (ConnectionRefusedError, ConnectionError, URLError, TimeoutError, socket.timeout)
    )


def run_pipeline(path: Path) -> PipelineStats:
    """Run import, Wiki/state persistence, and vector synchronization for one file."""
    result: ImportStats = import_file(path)
    vectors_updated = 0
    vector_status = "disabled"
    if _rag_enabled():
        try:
            vectors_updated = update_vector(
                path, db_path=Path(os.getenv("VECTOR_DB_PATH", str(DEFAULT_VECTOR_DB)))
            )
            vector_status = "updated"
        except Exception as exc:
            if _is_vector_unavailable(exc):
                vector_status = "unavailable"
                LOGGER.warning("Vector update unavailable; import was preserved: %s", exc)
            else:
                vector_status = "failed"
                LOGGER.exception("Vector update failed; import was preserved")
    return PipelineStats(
        read_markdown=result.read_markdown,
        gemini_calls=result.gemini_calls,
        processed=result.processed,
        skipped=result.skipped,
        failed=result.failed,
        vectors_updated=vectors_updated,
        imported=result.processed,
        vector_status=vector_status,
    )
