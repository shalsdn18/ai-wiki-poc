"""Typed contracts at the collector, LLM, and renderer boundaries."""

from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    url: str = Field(min_length=1)
    published_at: datetime
    source_tier: str = Field(min_length=1)

    @property
    def fingerprint(self) -> str:
        return sha256(self.content.encode("utf-8")).hexdigest()


class WikiUpdatePayload(BaseModel):
    """Structured output requested from the LLM; never Markdown."""

    current_state: str
    key_facts: list[str] = Field(default_factory=list)
    new_changes: list[str] = Field(default_factory=list)
    history_summary: str
    used_source_ids: list[str] = Field(default_factory=list)
