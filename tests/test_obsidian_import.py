from datetime import datetime, timezone
from pathlib import Path

from engine.obsidian_import import (
    category_hint,
    discover_markdown,
    import_vault,
    load_import_state,
)
from engine import obsidian_import
from engine.schemas import InboxKnowledgePayload


class FakeClassifier:
    def __init__(self):
        self.calls = 0
        self.hints = []

    def classify(self, source, category_hint=None):
        self.calls += 1
        self.hints.append(category_hint)
        primary = category_hint or "misc"
        return InboxKnowledgePayload(
            title=source.title,
            summary="Imported Obsidian knowledge.",
            key_facts=["Preserved fact"],
            primary_category=primary,
            categories=[primary],
            tags=["obsidian"],
            importance=3,
            source_url=None,
            topic=source.title,
            related_topics=["Personal Knowledge"],
        )


def test_vault_recursive_discovery_and_exclusions(tmp_path):
    vault = tmp_path / "Vault"
    included = [vault / "root.md", vault / "AI" / "nested.md"]
    excluded = [
        vault / ".obsidian" / "config.md",
        vault / ".trash" / "deleted.md",
        vault / "attachments" / "note.md",
        vault / "copilot" / "prompt.md",
        vault / "Archive" / "old.md",
        vault / ".agents" / "skill.md",
        vault / ".claude" / "command.md",
        vault / ".copilot" / "config.md",
        vault / ".opencode" / "agent.md",
        vault / "AI_Wiki" / "generated.md",
        vault / ".hidden" / "secret.md",
    ]
    for path in included + excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Note\nContent", encoding="utf-8")

    assert list(discover_markdown(vault)) == included


def test_reparse_point_directory_is_not_traversed(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    note = vault / "linked-output" / "generated.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Generated\nMust not be imported", encoding="utf-8")
    monkeypatch.setattr(
        obsidian_import,
        "_is_reparse_directory",
        lambda path: path.name == "linked-output",
    )

    assert list(discover_markdown(vault)) == []


def test_ai_and_investment_folder_hints():
    assert category_hint(Path("AI/RAG.md")) == "ai"
    assert category_hint(Path("투자/HBM.md")) == "investment"


def test_import_routes_nested_notes_and_preserves_originals(tmp_path):
    vault, wiki = tmp_path / "Vault", tmp_path / "wiki"
    ai_note = vault / "AI" / "RAG.md"
    investment_note = vault / "투자" / "HBM.md"
    generated_note = vault / "AI_Wiki" / "generated.md"
    for path, content in [
        (ai_note, "# RAG\nRetrieval notes"),
        (investment_note, "# HBM\nInvestment notes"),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    generated_note.parent.mkdir(parents=True)
    generated_note.write_text("# Generated Wiki\nOutput only", encoding="utf-8")
    originals = {path: path.read_bytes() for path in (ai_note, investment_note)}
    classifier = FakeClassifier()

    stats = import_vault(
        classifier,
        vault,
        wiki,
        tmp_path / ".obsidian_import_state.json",
        now=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert stats.read_markdown == 2
    assert stats.gemini_calls == 2
    assert stats.processed == 2
    assert stats.skipped == 0
    assert classifier.hints == ["ai", "investment"]
    assert (wiki / "ai" / "index.md").exists()
    assert (wiki / "investment" / "index.md").exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_unchanged_note_skips_and_changed_note_reprocesses(tmp_path):
    vault, wiki = tmp_path / "Vault", tmp_path / "wiki"
    note = vault / "AI" / "RAG.md"
    note.parent.mkdir(parents=True)
    note.write_text("# RAG\nVersion one", encoding="utf-8")
    state = tmp_path / ".obsidian_import_state.json"
    classifier = FakeClassifier()

    first = import_vault(classifier, vault, wiki, state)
    second = import_vault(classifier, vault, wiki, state)
    note.write_text("# RAG\nVersion two", encoding="utf-8")
    third = import_vault(classifier, vault, wiki, state)

    assert (first.processed, first.gemini_calls, first.skipped) == (1, 1, 0)
    assert (second.processed, second.gemini_calls, second.skipped) == (0, 0, 1)
    assert (third.processed, third.gemini_calls, third.skipped) == (1, 1, 0)
    assert classifier.calls == 2
    text = (wiki / "ai" / "index.md").read_text(encoding="utf-8")
    assert text.count("## RAG") == 1
    assert "source_id" not in text.split("---", 2)[-1]


def test_state_is_outside_vault(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    state = tmp_path / "project" / ".obsidian_import_state.json"
    import_vault(FakeClassifier(), vault, tmp_path / "wiki", state)
    assert state.exists()
    assert not (vault / ".obsidian_import_state.json").exists()
    document = next(iter(load_import_state(state)["documents"].values()))
    assert document["source_type"] == "obsidian"
    assert document["source_file"] == str(vault / "note.md")
    assert document["relative_path"] == "note.md"
    assert len(document["content_hash"]) == 64
    assert document["processed_at"]


def test_reads_vault_path_from_environment(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    stats = import_vault(
        FakeClassifier(),
        wiki_root=tmp_path / "wiki",
        state_path=tmp_path / ".obsidian_import_state.json",
    )

    assert stats.read_markdown == 1
    assert stats.processed == 1
