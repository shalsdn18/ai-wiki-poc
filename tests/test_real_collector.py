from collectors.real_collector import (
    CLAUDE_NEWS,
    GEMINI_FEED,
    OPENAI_FEED,
    collect_all,
    collect_claude,
    collect_gemini,
    collect_openai,
)

RSS = b"""<?xml version="1.0"?><rss><channel><item>
<title>Gemini API update</title><link>https://example.com/update/</link>
<description><![CDATA[<p>New Gemini feature.</p>]]></description>
<pubDate>Sun, 30 Aug 2026 09:00:00 GMT</pubDate>
</item></channel></rss>"""

ANTHROPIC = b"""<html><body><a href="/news/claude-update">
<time>Aug 29, 2026</time>
<h2>Claude update</h2><p>A new Claude capability.</p></a></body></html>"""


def test_rss_item_becomes_source_record_with_stable_url_id():
    first = collect_openai(lambda _: RSS)
    second = collect_openai(lambda _: RSS)
    assert first[0].source_id == second[0].source_id
    assert first[0].url == "https://example.com/update/"
    assert first[0].title == "Gemini API update"
    assert first[0].content == "New Gemini feature."
    assert first[0].published_at.isoformat() == "2026-08-30T09:00:00+00:00"
    assert first[0].source_tier == "official-openai-news"


def test_gemini_feed_filters_non_gemini_items():
    records = collect_gemini(lambda _: RSS)
    assert len(records) == 1
    assert records[0].source_tier == "official-google-gemini-blog"


def test_anthropic_html_becomes_source_record():
    records = collect_claude(lambda _: ANTHROPIC)
    assert len(records) == 1
    assert records[0].title == "Claude update"
    assert records[0].url == "https://www.anthropic.com/news/claude-update"
    assert records[0].source_tier == "official-anthropic-newsroom"


def test_provider_failure_does_not_stop_other_collectors():
    def fetch(url: str) -> bytes:
        if url == OPENAI_FEED:
            raise OSError("network unavailable")
        if url == GEMINI_FEED:
            return RSS
        if url == CLAUDE_NEWS:
            return ANTHROPIC
        raise AssertionError(url)

    result = collect_all(fetch)
    assert result["openai"] == []
    assert len(result["gemini"]) == 1
    assert len(result["claude"]) == 1
