from datetime import datetime, timezone

from engine.schemas import SourceRecord
from engine.wiki_poc import _analysis_prompt, render_markdown


def test_analysis_prompt_defaults_all_generated_fields_to_korean_and_preserves_source():
    source = SourceRecord(
        source_id="source-1",
        title="Original English title",
        content="OpenAI API release notes.",
        url="https://example.com/release",
        published_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        source_tier="official",
    )
    prompt = _analysis_prompt([source])
    assert "natural Korean (ko-KR), regardless of the source language" in prompt
    assert "Original English title" in prompt
    assert "https://example.com/release" in prompt
    assert "OpenAI API release notes" in prompt


def test_rendered_wiki_labels_and_display_date_are_korean():
    text = render_markdown(
        {
            "seen_hashes": [],
            "current_state": "주요 상태",
            "key_facts": ["핵심 사실"],
            "recent_changes": [
                {
                    "published_at": "2026-09-02T01:02:03+00:00",
                    "summary": "변경 내용",
                    "source_ids": ["source-1"],
                }
            ],
            "history_log": [],
        }
    )
    assert "## 현재 상태" in text
    assert "## 핵심 정보" in text
    assert "## 최근 변경 사항" in text
    assert "2026년 9월 2일 10:02 — 변경 내용 (출처: source-1)" in text
    assert '"published_at": "2026-09-02T01:02:03+00:00"' in text
