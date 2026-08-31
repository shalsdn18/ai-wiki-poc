"""Public, fictional sources used to exercise the PoC without web crawling."""

from datetime import datetime, timezone

from engine.schemas import SourceRecord


def collect() -> list[SourceRecord]:
    return [
        SourceRecord(
            source_id="mock-release-2026-08-30",
            title="Atlas API v2 fictional release",
            content="Fictional example: Atlas API v2 adds batch requests and changes the default request limit from 100 to 250 items.",
            url="https://example.com/atlas-api-v2",
            published_at=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc),
            source_tier="mock",
        ),
        SourceRecord(
            source_id="mock-policy-2026-08-31",
            title="Atlas retention policy fictional update",
            content="Fictional example: Atlas audit logs are retained for 90 days for newly created workspaces.",
            url="https://example.com/atlas-retention",
            published_at=datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc),
            source_tier="mock",
        ),
    ]
