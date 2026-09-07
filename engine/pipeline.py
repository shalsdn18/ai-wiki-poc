"""Single post-import pipeline shared by Watchdog and API callers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from engine.obsidian_import import ImportStats, import_file
from engine.vector_store import DEFAULT_VECTOR_DB, update_vector


@dataclass(frozen=True)
class PipelineStats:
    read_markdown: int
    gemini_calls: int
    processed: int
    skipped: int
    failed: int
    vectors_updated: int = 0


def run_pipeline(path: Path) -> PipelineStats:
    """Run import, Wiki/state persistence, and vector synchronization for one file."""
    result: ImportStats = import_file(path)
    vectors_updated = update_vector(
        path, db_path=Path(os.getenv("VECTOR_DB_PATH", str(DEFAULT_VECTOR_DB)))
    )
    return PipelineStats(
        read_markdown=result.read_markdown,
        gemini_calls=result.gemini_calls,
        processed=result.processed,
        skipped=result.skipped,
        failed=result.failed,
        vectors_updated=vectors_updated,
    )
