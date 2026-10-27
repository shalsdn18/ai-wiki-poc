"""Policy gate for a future explicit private-to-public publishing action."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet

from engine.knowledge_store import KnowledgeDocument


def select_explicitly_published(
    documents: Iterable[KnowledgeDocument], explicitly_selected_ids: AbstractSet[str]
) -> list[KnowledgeDocument]:
    """Return only explicitly selected public candidates; perform no publishing."""
    if not explicitly_selected_ids:
        return []
    return [
        document
        for document in documents
        if document.visibility == "public" and document.id in explicitly_selected_ids
    ]
