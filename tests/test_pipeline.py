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
    result = pipeline.run_pipeline(tmp_path / "note.md")

    assert result.processed == 1
    assert result.vectors_updated == 1
    assert [call[0] for call in calls] == ["import", "vector"]
