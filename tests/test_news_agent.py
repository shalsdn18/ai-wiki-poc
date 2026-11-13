from datetime import datetime, timedelta, timezone

import pytest

from engine import news_agent

UTC = timezone.utc  # noqa: UP017 - retain Python 3.10 test compatibility

RSS = b"""<?xml version="1.0"?><rss><channel><item>
<title>New AI release</title><link>https://example.com/new</link>
<description>Useful release details.</description><pubDate>Mon, 01 Sep 2026 12:00:00 GMT</pubDate>
</item></channel></rss>"""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "2026-09-01T12:00:00Z",
            datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        ),
        (
            "2026-09-01T12:00:00+05:30",
            datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        ),
        (
            "2026-09-01T12:00:00",
            datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        ),
    ],
)
def test_published_iso_dates_preserve_timezone_semantics(value, expected):
    assert news_agent._published(value) == expected


def test_parse_feed_and_collect_once_deduplicates_urls(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    db = tmp_path / "news.db"
    fetches = []

    def fetch(url):
        fetches.append(url)
        return RSS

    original = news_agent.FEEDS.copy()
    news_agent.FEEDS.clear()
    news_agent.FEEDS["openai"] = "https://example.com/rss"
    try:
        assert news_agent.collect_once(vault, db, fetch=fetch) == 1
        assert news_agent.collect_once(vault, db, fetch=fetch) == 0
    finally:
        news_agent.FEEDS.clear()
        news_agent.FEEDS.update(original)

    files = list((vault / "Inbox").glob("*.md"))
    assert len(files) == 1
    assert "# New AI release" in files[0].read_text(encoding="utf-8")
    assert len(fetches) == 2


def test_collect_once_retries_failed_feed(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    attempts = []
    original = news_agent.FEEDS.copy()
    news_agent.FEEDS.clear()
    news_agent.FEEDS["openai"] = "https://example.com/rss"
    try:

        def fetch(_):
            attempts.append(True)
            if len(attempts) < 2:
                raise OSError("temporary failure")
            return RSS

        delays = []
        assert (
            news_agent.collect_once(vault, tmp_path / "news.db", fetch=fetch, sleep=delays.append)
            == 1
        )
        assert len(attempts) == 2
        assert delays == [1]
    finally:
        news_agent.FEEDS.clear()
        news_agent.FEEDS.update(original)


def test_news_note_localizes_labels_and_date_but_preserves_source_title():
    item = news_agent.parse_feed(RSS, "openai", "https://example.com/", limit=1)[0]
    markdown = news_agent._markdown(item)
    assert "# New AI release" in markdown
    assert "출처 URL: https://example.com/new" in markdown
    assert "게시 시각: 2026년 9월 1일 21:00" in markdown
