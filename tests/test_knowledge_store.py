from datetime import date

import pytest

from engine.knowledge_store import (
    FilesystemKnowledgeStore,
    KnowledgeDocumentError,
)


def _write_note(
    root, category, name, *, title=None, visibility="private", updated="2026-01-01", extra=""
):
    directory = root / category
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title or name.removesuffix('.md')}\n"
        f"category: {category}\n"
        "tags: [sample, test]\n"
        'created: "2025-01-01"\n'
        f'updated: "{updated}"\n'
        f"visibility: {visibility}\n"
        f"{extra}"
        "---\n\nA body with searchable knowledge.\n",
        encoding="utf-8",
    )
    return path


def test_parse_required_metadata_and_optional_source_url(tmp_path):
    _write_note(tmp_path, "ai", "example.md", extra="source_url: https://example.com\n")

    note = FilesystemKnowledgeStore(tmp_path).list_documents()[0]

    assert note.title == "example"
    assert note.category == "ai"
    assert note.tags == ["sample", "test"]
    assert note.created == "2025-01-01"
    assert note.updated == "2026-01-01"
    assert note.visibility == "private"
    assert note.source_url == "https://example.com"
    assert note.content == "A body with searchable knowledge."


@pytest.mark.parametrize(
    "category, extra, expected",
    [
        ("unknown", "", "category"),
        ("ai", "visibility: internal\n", "visibility"),
        ("ai", "", ""),
    ],
)
def test_reject_invalid_category_visibility_and_missing_metadata(
    tmp_path, category, extra, expected
):
    _write_note(tmp_path, category, "invalid.md", extra=extra)
    path = tmp_path / category / "invalid.md"
    if expected == "":
        raw = path.read_text(encoding="utf-8").replace("tags: [sample, test]\n", "")
        path.write_text(raw, encoding="utf-8")

    with pytest.raises(KnowledgeDocumentError):
        FilesystemKnowledgeStore(tmp_path).list_documents()


def test_stable_id_uses_relative_path_not_machine_path(tmp_path):
    first_root = tmp_path / "one" / "clone" / "knowledge"
    second_root = tmp_path / "another" / "machine" / "knowledge"
    _write_note(first_root, "ai", "nested/example.md")
    _write_note(second_root, "ai", "nested/example.md")

    first = FilesystemKnowledgeStore(first_root).list_documents()[0]
    second = FilesystemKnowledgeStore(second_root).list_documents()[0]

    assert first.id == second.id
    assert str(tmp_path).casefold() not in first.id.casefold()
    assert len(first.id) == 64


def test_stats_recent_ordering_and_keyword_search(tmp_path):
    _write_note(tmp_path, "ai", "older.md", title="Old", updated="2025-01-01")
    _write_note(tmp_path, "investment", "newer.md", title="New", updated="2026-06-01")
    store = FilesystemKnowledgeStore(tmp_path)

    notes = store.list_documents()
    assert [note.title for note in notes] == ["New", "Old"]
    assert store.stats() == {
        "documents": 2,
        "categories": {"ai": 1, "investment": 1, "philosophy": 0, "misc": 0},
    }
    assert [note.title for note in store.search("SEARCHABLE")] == ["New", "Old"]
    assert store.get_document(notes[0].id).relative_path == "investment/newer.md"


def test_iso_dates_are_platform_independent(tmp_path):
    _write_note(tmp_path, "ai", "date.md")
    note = FilesystemKnowledgeStore(tmp_path).list_documents()[0]
    assert date.fromisoformat(note.created).isoformat() == "2025-01-01"
