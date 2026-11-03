"""Read-only reconciliation classifications for R2 and PostgreSQL inventories."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Classification(StrEnum):
    HEALTHY = "HEALTHY"
    AUTO_REPAIR = "AUTO_REPAIR"
    QUARANTINE = "QUARANTINE"
    GC_CANDIDATE = "GC_CANDIDATE"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class RevisionInventory:
    source_exists: bool
    manifest_exists: bool
    database_revision_exists: bool
    intent_exists: bool
    current_pointer_valid: bool = True
    job_revision_valid: bool = True
    intent_stale: bool = False
    hash_valid: bool = True


def classify_revision(item: RevisionInventory) -> Classification:
    """Classify one exact revision inventory tuple; never deletes or repairs."""
    if not item.current_pointer_valid or not item.job_revision_valid:
        return Classification.MANUAL
    if item.intent_stale:
        return Classification.MANUAL
    if item.database_revision_exists:
        if not item.source_exists or not item.manifest_exists or not item.hash_valid:
            return Classification.MANUAL
        return Classification.HEALTHY
    if item.source_exists and item.manifest_exists:
        if item.hash_valid and item.intent_exists:
            return Classification.AUTO_REPAIR
        return Classification.QUARANTINE
    if item.source_exists and not item.manifest_exists:
        return Classification.QUARANTINE if not item.intent_exists else Classification.AUTO_REPAIR
    if item.manifest_exists and not item.source_exists:
        return Classification.MANUAL
    if item.intent_exists:
        return Classification.AUTO_REPAIR
    return Classification.GC_CANDIDATE


def classify_write_intent(
    state: str, age_seconds: float, stale_after_seconds: float
) -> Classification:
    if state in {"COMMITTED", "CONFLICT", "QUARANTINED"}:
        return Classification.HEALTHY if state == "COMMITTED" else Classification.MANUAL
    if age_seconds < stale_after_seconds:
        return Classification.AUTO_REPAIR
    return Classification.MANUAL
