from datetime import datetime, timedelta, timezone

from engine.schemas import SourceRecord, WikiUpdatePayload
from engine.wiki_poc import load_state, run_pipeline


class FakeAnalyzer:
    def __init__(self, payload: WikiUpdatePayload) -> None:
        self.payload = payload
        self.calls = 0

    def analyze(self, sources):
        self.calls += 1
        return self.payload


def source(index: int) -> SourceRecord:
    return SourceRecord(
        source_id=f"source-{index}", title=f"Source {index}", content=f"Unique content {index}",
        url=f"https://example.com/{index}",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
        source_tier="mock",
    )


def payload(index: int, used_ids: list[str] | None = None) -> WikiUpdatePayload:
    return WikiUpdatePayload(
        current_state=f"State {index}", key_facts=[f"Fact {index}"], new_changes=[f"Change {index}"],
        history_summary=f"History {index}",
        used_source_ids=used_ids if used_ids is not None else [f"source-{index}"],
    )


def test_new_source_creates_markdown(tmp_path):
    path = tmp_path / "wiki" / "ai-wiki.md"
    analyzer = FakeAnalyzer(payload(1))
    assert run_pipeline([source(1)], analyzer, path) is True
    assert path.exists()
    assert "# AI Wiki" in path.read_text(encoding="utf-8")
    assert analyzer.calls == 1


def test_same_source_is_blocked_by_delta_gate(tmp_path):
    path = tmp_path / "wiki.md"
    analyzer = FakeAnalyzer(payload(1))
    record = source(1)
    assert run_pipeline([record], analyzer, path) is True
    original = path.read_text(encoding="utf-8")
    assert run_pipeline([record], analyzer, path) is False
    assert path.read_text(encoding="utf-8") == original
    assert analyzer.calls == 1


def test_invalid_used_source_id_is_filtered(tmp_path):
    path = tmp_path / "wiki.md"
    run_pipeline([source(1)], FakeAnalyzer(payload(1, ["source-1", "invented-id"])), path)
    state = load_state(path)
    assert state["recent_changes"][0]["source_ids"] == ["source-1"]
    assert state["history_log"][0]["source_ids"] == ["source-1"]
    assert "invented-id" not in path.read_text(encoding="utf-8")


def test_recent_changes_keeps_latest_five(tmp_path):
    path = tmp_path / "wiki.md"
    for index in range(1, 8):
        run_pipeline([source(index)], FakeAnalyzer(payload(index)), path)
    changes = load_state(path)["recent_changes"]
    assert len(changes) == 5
    assert [entry["summary"] for entry in changes] == [
        "Change 7", "Change 6", "Change 5", "Change 4", "Change 3"
    ]
