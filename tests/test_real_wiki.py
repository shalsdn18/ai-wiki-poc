import logging

import pytest
from google.genai.errors import APIError

from engine import real_wiki
from engine.schemas import SourceRecord, WikiUpdatePayload


class FakeAnalyzer:
    def analyze(self, sources):
        return WikiUpdatePayload(
            current_state="Current",
            key_facts=["Fact"],
            new_changes=["Change"],
            history_summary="History",
            used_source_ids=[source.source_id for source in sources],
        )


def test_each_provider_uses_its_own_markdown_file(tmp_path, monkeypatch):
    from collectors.mock_collector import collect

    records = collect()
    monkeypatch.setattr(real_wiki, "collect_all", lambda: {
        "openai": [records[0]], "gemini": [records[1]], "claude": [records[0]]
    })
    monkeypatch.setattr(real_wiki, "OUTPUTS", {
        name: tmp_path / f"{name}.md" for name in ("openai", "gemini", "claude")
    })

    assert real_wiki.update_all(FakeAnalyzer()) == {
        "openai": True, "gemini": True, "claude": True
    }
    assert all((tmp_path / f"{name}.md").exists() for name in ("openai", "gemini", "claude"))


def _configure_provider_test(tmp_path, monkeypatch):
    from collectors.mock_collector import collect

    record = collect()[0]
    monkeypatch.setattr(real_wiki, "collect_all", lambda: {
        name: [record] for name in ("openai", "gemini", "claude")
    })
    monkeypatch.setattr(real_wiki, "OUTPUTS", {
        name: tmp_path / f"{name}.md" for name in ("openai", "gemini", "claude")
    })


def _api_error(code: int) -> APIError:
    return APIError(code, {"message": f"failure-{code}"})


def test_transient_error_retries_with_exponential_backoff(tmp_path, monkeypatch):
    _configure_provider_test(tmp_path, monkeypatch)
    attempts = {name: 0 for name in ("openai", "gemini", "claude")}
    sleeps = []

    def fake_run(sources, analyzer, path):
        provider = path.stem
        attempts[provider] += 1
        if provider == "openai" and attempts[provider] < 3:
            raise _api_error(429 if attempts[provider] == 1 else 503)
        return True

    monkeypatch.setattr(real_wiki, "run_pipeline", fake_run)
    results = real_wiki.update_all(FakeAnalyzer(), sleep=sleeps.append)

    assert results == {"openai": True, "gemini": True, "claude": True}
    assert attempts == {"openai": 3, "gemini": 1, "claude": 1}
    assert sleeps == [1.0, 2.0]


def test_final_provider_failure_is_logged_and_others_continue(tmp_path, monkeypatch, caplog):
    _configure_provider_test(tmp_path, monkeypatch)
    attempts = {name: 0 for name in ("openai", "gemini", "claude")}

    def fake_run(sources, analyzer, path):
        provider = path.stem
        attempts[provider] += 1
        if provider == "gemini":
            raise _api_error(500)
        return True

    monkeypatch.setattr(real_wiki, "run_pipeline", fake_run)
    with caplog.at_level(logging.ERROR, logger=real_wiki.__name__):
        results = real_wiki.update_all(FakeAnalyzer(), sleep=lambda _: None)

    assert results == {"openai": True, "gemini": False, "claude": True}
    assert attempts == {"openai": 1, "gemini": 3, "claude": 1}
    assert "gemini provider failed" in caplog.text
    assert "HTTP 500" in caplog.text


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_error_fails_immediately(
    status_code, tmp_path, monkeypatch, caplog
):
    _configure_provider_test(tmp_path, monkeypatch)
    attempts = {name: 0 for name in ("openai", "gemini", "claude")}
    sleeps = []

    def fake_run(sources, analyzer, path):
        provider = path.stem
        attempts[provider] += 1
        if provider == "openai":
            raise _api_error(status_code)
        return True

    monkeypatch.setattr(real_wiki, "run_pipeline", fake_run)
    with caplog.at_level(logging.ERROR, logger=real_wiki.__name__):
        results = real_wiki.update_all(FakeAnalyzer(), sleep=sleeps.append)

    assert results == {"openai": False, "gemini": True, "claude": True}
    assert attempts == {"openai": 1, "gemini": 1, "claude": 1}
    assert sleeps == []
    assert f"HTTP {status_code}" in caplog.text
