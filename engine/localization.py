"""Korean defaults for human-readable knowledge output."""

from datetime import datetime, timedelta, timezone

DEFAULT_LOCALE = "ko-KR"
KOREA_TIMEZONE = timezone(timedelta(hours=9), name="KST")
CATEGORY_LABELS = {
    "ai": "AI",
    "investment": "투자",
    "philosophy": "철학",
    "misc": "기타",
}


def category_label(category: str) -> str:
    """Translate a category code for display without changing its stored value."""
    return CATEGORY_LABELS.get(category, category)


def format_datetime_ko(value: str) -> str:
    """Format an ISO timestamp as a Korean local time while preserving stored ISO data."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(KOREA_TIMEZONE)
    return f"{local.year}년 {local.month}월 {local.day}일 {local.hour:02d}:{local.minute:02d}"
