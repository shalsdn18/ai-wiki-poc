import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from engine.inbox_wiki import _validated_payload, process_inbox
from engine.schemas import InboxKnowledgePayload


class FakeClassifier:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def classify(self, source):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


def payload(primary, categories=None):
    return InboxKnowledgePayload(
        title=f"{primary.title()} knowledge",
        summary="A concise summary.",
        key_facts=["Fact one", "Fact two"],
        primary_category=primary,
        categories=categories or [primary],
        tags=[primary, "knowledge"],
        importance=4,
        source_url="https://llm-invented.example/wrong",
        topic=f"{primary.title()} topic",
        related_topics=["Related knowledge"],
    )


def write_inbox(root, name="item.md", url="https://example.com/article", body="Saved article text"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(
        f"---\ntype: article\nsource_url: {url}\ntitle: Optional title\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("category", ["ai", "investment", "philosophy", "misc"])
def test_single_category_routes_to_category_wiki(category, tmp_path):
    inbox = tmp_path / "inbox"
    wiki = tmp_path / "wiki"
    write_inbox(inbox)
    result = process_inbox(FakeClassifier(payload(category)), inbox, wiki)
    assert result == {"item.md": "processed"}
    output = wiki / category / "index.md"
    assert output.exists()
    assert f"# {category.title()} Knowledge Wiki" in output.read_text(encoding="utf-8")


def test_ai_and_investment_keeps_one_primary_and_multiple_categories(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    process_inbox(FakeClassifier(payload("investment", ["investment", "ai"])), inbox, wiki)
    text = (wiki / "investment" / "index.md").read_text(encoding="utf-8")
    assert "Categories: investment, ai" in text
    assert not (wiki / "ai" / "index.md").exists()


def test_duplicate_url_does_not_call_gemini_again(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    classifier = FakeClassifier(payload("ai"))
    assert process_inbox(classifier, inbox, wiki)["item.md"] == "processed"
    assert process_inbox(classifier, inbox, wiki)["item.md"] == "duplicate"
    assert classifier.calls == 1


def test_duplicate_does_not_even_construct_gemini_client(tmp_path, monkeypatch):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    process_inbox(FakeClassifier(payload("ai")), inbox, wiki)

    def unexpected_client():
        raise AssertionError("Gemini client must stay behind the Delta Gate")

    monkeypatch.setattr("engine.inbox_wiki.GeminiInboxClassifier", unexpected_client)
    assert process_inbox(None, inbox, wiki) == {"item.md": "duplicate"}


def test_same_content_at_different_url_is_duplicate(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox, "one.md", "https://example.com/one", "Same content")
    classifier = FakeClassifier(payload("ai"))
    process_inbox(classifier, inbox, wiki)
    write_inbox(inbox, "two.md", "https://example.com/two", "Same content")
    result = process_inbox(classifier, inbox, wiki)
    assert result["two.md"] == "duplicate"
    assert classifier.calls == 1


def test_invalid_category_fails_validation_and_is_not_processed(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    invalid = {
        "title": "Bad",
        "summary": "Bad category",
        "key_facts": [],
        "primary_category": "sports",
        "categories": ["sports"],
        "tags": [],
        "importance": 3,
        "source_url": None,
    }
    result = process_inbox(FakeClassifier(invalid), inbox, wiki)
    assert result == {"item.md": "failed"}
    assert not (inbox / ".processed.json").exists()


def test_primary_category_missing_from_categories_is_invalid():
    with pytest.raises(ValidationError):
        InboxKnowledgePayload(
            title="Mixed",
            summary="Summary",
            key_facts=[],
            primary_category="ai",
            categories=["investment"],
            tags=[],
            importance=3,
        )


def test_corrupted_korean_payload_is_rejected():
    value = payload("ai").model_dump()
    value["summary"] = "ìž˜ëª»ë'œ ì„¤ëª…"
    with pytest.raises(ValueError, match="suspicious encoding"):
        _validated_payload(value, None)


def test_empty_summary_is_rejected():
    value = payload("ai").model_dump()
    value["summary"] = "   "
    with pytest.raises(ValueError, match="summary"):
        _validated_payload(value, None)


def test_valid_korean_payload_is_accepted():
    value = payload("ai").model_copy(
        update={
            "title": "생성형 AI 개요",
            "summary": "생성형 AI의 핵심 개념을 설명합니다.",
            "key_facts": ["모델은 문맥을 기반으로 응답을 생성합니다."],
            "topic": "생성형 AI",
        }
    )
    assert _validated_payload(value, None).title == "생성형 AI 개요"


def test_gemini_failure_does_not_mark_processed(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    result = process_inbox(FakeClassifier(error=RuntimeError("Gemini unavailable")), inbox, wiki)
    assert result == {"item.md": "failed"}
    assert not (inbox / ".processed.json").exists()
    assert not (wiki / "ai" / "index.md").exists()


def test_provenance_preserved_but_internal_hash_not_rendered(tmp_path):
    inbox, wiki = tmp_path / "inbox", tmp_path / "wiki"
    write_inbox(inbox)
    fixed = datetime(2026, 9, 2, 1, 2, 3, tzinfo=timezone.utc)
    process_inbox(FakeClassifier(payload("ai")), inbox, wiki, now=lambda: fixed)
    text = (wiki / "ai" / "index.md").read_text(encoding="utf-8")
    state = json.loads((inbox / ".processed.json").read_text(encoding="utf-8"))
    item = next(iter(state["processed_sources"].values()))
    assert item["input_filename"] == "item.md"
    assert item["processed_at"] == fixed.isoformat()
    assert item["source_url"] == "https://example.com/article"
    assert item["content_hash"] not in text
    assert "https://llm-invented.example/wrong" not in text
    assert "https://example.com/article" in text
