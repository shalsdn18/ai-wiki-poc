from datetime import datetime, timezone
from pathlib import Path

from engine.obsidian_import import (
    category_hint,
    discover_markdown,
    import_file,
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
        vault / "MOC_AI & LLM.md",
        vault / "README.md",
        vault / "Template.md",
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


def test_import_skips_moc_readme_and_template_markdown(tmp_path):
    vault, wiki = tmp_path / "Vault", tmp_path / "wiki"
    included = vault / "AI" / "RAG.md"
    skipped = [
        vault / "MOC_AI & LLM.md",
        vault / "README.md",
        vault / "Template.md",
    ]
    included.parent.mkdir(parents=True, exist_ok=True)
    included.write_text("# RAG\nKept", encoding="utf-8")
    for path in skipped:
        path.write_text("# Skip\nIgnored", encoding="utf-8")
    classifier = FakeClassifier()

    stats = import_vault(
        classifier,
        vault,
        wiki,
        tmp_path / ".obsidian_import_state.json",
        now=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert stats.read_markdown == 1
    assert stats.gemini_calls == 1
    assert stats.processed == 1
    assert stats.skipped == 0
    assert classifier.calls == 1
    assert list(load_import_state(tmp_path / ".obsidian_import_state.json")["documents"]) == [
        "AI/RAG.md"
    ]


def test_import_preserves_korean_filename_content_and_bom(tmp_path):
    vault, wiki = tmp_path / "Vault", tmp_path / "wiki"
    note = vault / "GPT-5.6 사이버 및 데이브레이크.md"
    note.parent.mkdir(parents=True)
    note.write_bytes("# GPT-5.6 사이버 및 데이브레이크\n한글 본문입니다.".encode("utf-8-sig"))

    class KoreanClassifier(FakeClassifier):
        def classify(self, source, category_hint=None):
            self.calls += 1
            return InboxKnowledgePayload(
                title=source.title,
                summary=source.content.splitlines()[1],
                key_facts=["한글 핵심 사실"],
                primary_category="misc",
                categories=["misc"],
                tags=["한글"],
                importance=3,
                source_url=None,
                topic=source.title,
                related_topics=[],
            )

    state = tmp_path / ".obsidian_import_state.json"
    stats = import_vault(KoreanClassifier(), vault, wiki, state)

    assert stats.read_markdown == 1
    assert stats.gemini_calls == 1
    assert stats.processed == 1
    document = next(iter(load_import_state(state)["documents"].values()))
    assert document["relative_path"] == "GPT-5.6 사이버 및 데이브레이크.md"
    rendered = (wiki / "misc" / "index.md").read_text(encoding="utf-8")
    assert "## GPT-5.6 사이버 및 데이브레이크" in rendered
    assert "한글 본문입니다." in rendered


class GeminiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.code = status


def _single_note(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    return vault


def test_import_applies_delay_between_classify_calls(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    for name in ("one.md", "two.md"):
        (vault / name).write_text(f"# {name}\nContent", encoding="utf-8")
    delays = []
    monkeypatch.setattr(obsidian_import.time, "sleep", delays.append)

    stats = import_vault(
        FakeClassifier(), vault, tmp_path / "wiki", tmp_path / "state.json", delay_seconds=5
    )

    assert stats.processed == 2
    assert delays == [5]


def test_import_retries_429_using_retry_delay_and_processes(tmp_path, monkeypatch):
    class RetryClassifier(FakeClassifier):
        def classify(self, source, category_hint=None):
            self.calls += 1
            if self.calls == 1:
                raise GeminiError(429, "RESOURCE_EXHAUSTED retryDelay: 7s")
            return super().classify(source, category_hint)

    delays = []
    monkeypatch.setattr(obsidian_import.time, "sleep", delays.append)
    state = tmp_path / "state.json"
    stats = import_vault(RetryClassifier(), _single_note(tmp_path), tmp_path / "wiki", state, delay_seconds=0)

    assert (stats.processed, stats.failed) == (1, 0)
    assert delays == [7.0]
    assert "note.md" in load_import_state(state)["documents"]


def test_import_retries_503_with_exponential_backoff(tmp_path, monkeypatch):
    class RetryClassifier(FakeClassifier):
        def classify(self, source, category_hint=None):
            self.calls += 1
            if self.calls <= 3:
                raise GeminiError(503, "UNAVAILABLE")
            return super().classify(source, category_hint)

    delays = []
    monkeypatch.setattr(obsidian_import.time, "sleep", delays.append)
    stats = import_vault(
        RetryClassifier(), _single_note(tmp_path), tmp_path / "wiki", tmp_path / "state.json",
        delay_seconds=0, max_retries=3,
    )

    assert stats.processed == 1
    assert delays == [30.0, 60.0, 120.0]


def test_import_does_not_record_document_after_retry_failure(tmp_path, monkeypatch):
    class FailingClassifier(FakeClassifier):
        def classify(self, source, category_hint=None):
            self.calls += 1
            raise GeminiError(503, "UNAVAILABLE")

    delays = []
    monkeypatch.setattr(obsidian_import.time, "sleep", delays.append)
    state = tmp_path / "state.json"
    stats = import_vault(
        FailingClassifier(), _single_note(tmp_path), tmp_path / "wiki", state,
        delay_seconds=0, max_retries=2,
    )

    assert (stats.processed, stats.failed) == (0, 1)
    assert delays == [30.0, 60.0]
    assert load_import_state(state)["documents"] == {}


def test_import_file_processes_only_target_and_skips_unchanged(tmp_path, monkeypatch):
    vault = _single_note(tmp_path)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    other = vault / "other.md"
    other.write_text("# Other\nContent", encoding="utf-8")
    state = tmp_path / "state.json"
    wiki = tmp_path / "wiki"
    classifier = FakeClassifier()

    first = import_file(vault / "note.md", classifier, wiki, state, delay_seconds=0)
    second = import_file(vault / "note.md", classifier, wiki, state, delay_seconds=0)

    assert (first.processed, first.skipped) == (1, 0)
    assert (second.processed, second.skipped) == (0, 1)
    assert classifier.calls == 1
    assert set(load_import_state(state)["documents"]) == {"note.md"}
    assert not (wiki / "misc" / "index.md").read_text(encoding="utf-8").count("## Other")


def test_import_file_uses_llm_cache_before_classifier(tmp_path, monkeypatch):
    vault = _single_note(tmp_path)
    cached_file = vault / "cached.md"
    cached_file.write_text("# Note\nContent", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    classifier = FakeClassifier()
    cache_path = tmp_path / "cache.db"

    first = import_file(vault / "note.md", classifier, tmp_path / "wiki", tmp_path / "state.json", cache_path=cache_path)
    second = import_file(cached_file, classifier, tmp_path / "wiki", tmp_path / "state.json", cache_path=cache_path)

    assert (first.gemini_calls, second.gemini_calls) == (1, 0)
    assert classifier.calls == 1
    assert second.processed == 1
