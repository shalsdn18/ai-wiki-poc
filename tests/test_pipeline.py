import pytest

from engine import pipeline


def test_run_pipeline_calls_import_then_vector_update(monkeypatch, tmp_path):
    calls = []

    def fake_import(path):
        calls.append(("import", path))
        return pipeline.ImportStats(read_markdown=1, gemini_calls=1, processed=1)

    def fake_update(path, db_path):
        calls.append(("vector", path, db_path))
        return 1

    monkeypatch.setattr(pipeline, "import_file", fake_import)
    monkeypatch.setattr(pipeline, "update_vector", fake_update)
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "true")
    result = pipeline.run_pipeline(tmp_path / "note.md")

    assert result.processed == 1
    assert result.vectors_updated == 1
    assert [call[0] for call in calls] == ["import", "vector"]


def test_rag_disabled_does_not_call_ollama(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "import_file", lambda path: pipeline.ImportStats(processed=1))
    monkeypatch.setattr(pipeline, "update_vector", lambda *args, **kwargs: pytest.fail("called"))
    result = pipeline.run_pipeline(tmp_path / "note.md")
    assert result.vector_status == "disabled"
    assert result.vectors_updated == 0


def test_unavailable_vector_does_not_fail_import(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "true")
    monkeypatch.setattr(pipeline, "import_file", lambda path: pipeline.ImportStats(processed=1))

    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError()

    monkeypatch.setattr(pipeline, "update_vector", unavailable)
    result = pipeline.run_pipeline(tmp_path / "note.md")
    assert result.failed == 0
    assert result.vector_status == "unavailable"


def test_unexpected_vector_error_is_logged_and_import_preserved(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "true")
    monkeypatch.setattr(pipeline, "import_file", lambda path: pipeline.ImportStats(processed=1))

    def corrupt(*args, **kwargs):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(pipeline, "update_vector", corrupt)
    result = pipeline.run_pipeline(tmp_path / "note.md")
    assert result.vector_status == "failed"
    assert "Vector update failed" in caplog.text
