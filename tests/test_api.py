import json

from fastapi.testclient import TestClient

import app
from engine.pipeline import PipelineStats


def _write_fixture(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    wiki_root = tmp_path / "wiki"
    entry = {
        "source_id": "obsidian-1",
        "title": "한글 노트",
        "summary": "검색 가능한 요약",
        "relative_path": "AI/한글.md",
        "primary_category": "ai",
        "processed_at": "2026-09-07T00:00:00+00:00",
        "key_facts": ["중요 사실"],
        "tags": ["한글"],
        "topic": "테스트 주제",
    }
    state_path.write_text(
        json.dumps({"version": 1, "documents": {"AI/한글.md": entry}}, ensure_ascii=False),
        encoding="utf-8",
    )
    wiki_path = wiki_root / "ai" / "index.md"
    wiki_path.parent.mkdir(parents=True)
    wiki_path.write_text(
        "---\n" + json.dumps({"entries": [entry]}, ensure_ascii=False) + "\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OBSIDIAN_IMPORT_STATE_PATH", str(state_path))
    monkeypatch.setenv("WIKI_ROOT", str(wiki_root))


def test_api_stats_recent_search_and_note(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    client = TestClient(app.app)

    assert client.get("/stats").json() == {
        "documents": 1,
        "categories": {"ai": 1, "investment": 0, "philosophy": 0, "misc": 0},
    }
    assert client.get("/recent").json()[0]["title"] == "한글 노트"
    assert client.get("/search", params={"q": "검색 가능한"}).json()[0]["source_id"] == "obsidian-1"
    assert client.get("/note/obsidian-1").json()["relative_path"] == "AI/한글.md"
    assert client.get("/note/missing").status_code == 404


def test_api_import_delegates_to_existing_importer(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    calls = []

    def fake_pipeline(path):
        calls.append(path)
        return PipelineStats(read_markdown=1, gemini_calls=1, processed=1, skipped=0, failed=0)

    monkeypatch.setattr(app, "run_pipeline", fake_pipeline)
    response = TestClient(app.app).post("/import")

    assert response.status_code == 200
    assert response.json()["processed"] == 1
    assert calls == [vault / "note.md"]


def test_api_semantic_search(monkeypatch):
    monkeypatch.setattr(
        app,
        "semantic_search",
        lambda query, db_path, limit: [app.SemanticResult("Note", 0.9, "snippet", "source-1")],
    )

    response = TestClient(app.app).get("/semantic-search", params={"q": "meaning"})

    assert response.status_code == 200
    assert response.json() == [
        {"title": "Note", "score": 0.9, "snippet": "snippet", "id": "source-1"}
    ]


def test_api_chat_uses_top_semantic_context_and_returns_sources(monkeypatch):
    matches = [
        app.SemanticResult(f"Note {index}", 1 - index / 10, f"snippet {index}", f"source-{index}")
        for index in range(5)
    ]
    captured = {}
    monkeypatch.setattr(app, "semantic_search", lambda query, db_path, limit: matches)
    monkeypatch.setattr(
        app, "get_documents", lambda ids, db_path: {ids[0]: "Full document context"}
    )

    def fake_answer(question, context):
        captured["question"] = question
        captured["context"] = context
        return "Grounded answer"

    monkeypatch.setattr(app, "_generate_chat_answer", fake_answer)
    response = TestClient(app.app).post("/chat", json={"question": "What is relevant?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "Grounded answer"
    assert len(response.json()["sources"]) == 5
    assert captured == {
        "question": "What is relevant?",
        "context": "[source-0] Note 0\nFull document context\n\n[source-1] Note 1\nsnippet 1\n\n[source-2] Note 2\nsnippet 2\n\n[source-3] Note 3\nsnippet 3\n\n[source-4] Note 4\nsnippet 4",
    }
