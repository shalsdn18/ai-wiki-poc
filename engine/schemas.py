"""Typed contracts at the collector, LLM, and renderer boundaries."""

from datetime import datetime
from hashlib import sha256

from typing import Literal

from pydantic import BaseModel, Field, model_validator

KnowledgeCategory = Literal["ai", "investment", "philosophy", "misc"]


class SourceRecord(BaseModel):
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    url: str = Field(min_length=1)
    published_at: datetime
    source_tier: str = Field(min_length=1)
    source_type: str | None = None

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


class InboxKnowledgePayload(BaseModel):
    """Structured classification/extraction result for a user inbox document."""

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    key_facts: list[str] = Field(default_factory=list)
    primary_category: KnowledgeCategory
    categories: list[KnowledgeCategory] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5)
    source_url: str | None = None
    topic: str = Field(min_length=1)
    related_topics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def primary_category_must_be_in_categories(self) -> "InboxKnowledgePayload":
        if self.primary_category not in self.categories:
            raise ValueError("primary_category must also appear in categories")
        return self
