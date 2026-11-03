import json

from fastapi.testclient import TestClient

import app
from engine.pipeline import PipelineStats
from engine.vector_store import SemanticResult


def test_chat_prompt_defaults_to_korean_and_preserves_source_titles():
    prompt = app._chat_prompt(
        "What is in the OpenAI note?", "[source-1] OpenAI API overview\nDetails"
    )
    assert "natural Korean (ko-KR)" in prompt
    assert "cite the relevant document titles exactly as written" in prompt
    assert "[source-1] OpenAI API overview" in prompt


def _write_fixture(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    wiki_root = tmp_path / "wiki"
    entry = {
        "source_id": "obsidian-1",
        "title": "한글 노트",
        "summary": "검색 가능한 요약",
        "relative_path": "C:/Users/private/Vault/AI/한글.md",
        "source_file": "C:/Users/private/Vault/AI/한글.md",
        "input_filename": "한글.md",
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
    note = client.get("/note/obsidian-1").json()
    assert "relative_path" not in note
    assert "source_file" not in note
    assert "input_filename" not in note
    assert client.get("/search", params={"q": "C:/Users/private"}).json() == []
    assert client.get("/note/missing").status_code == 404


def test_public_wiki_api_contract_is_unchanged_by_knowledge_api(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    client = TestClient(app.app)

    assert client.get("/stats").json()["documents"] == 1
    assert client.get("/recent").json()[0]["source_id"] == "obsidian-1"
    assert client.get("/search", params={"q": "검색 가능한"}).status_code == 200
    assert client.get("/note/obsidian-1").status_code == 200
    assert "relative_path" not in client.get("/recent").json()[0]


def test_public_wiki_empty_state_and_removed_note_semantics(tmp_path, monkeypatch):
    wiki_root = tmp_path / "wiki"
    for category in ("ai", "investment", "philosophy", "misc"):
        path = wiki_root / category / "index.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'---\n{{"category":"{category}","entries":[]}}\n---\n',
            encoding="utf-8",
        )
    monkeypatch.setenv("WIKI_ROOT", str(wiki_root))
    client = TestClient(app.app)

    assert client.get("/stats").json() == {
        "documents": 0,
        "categories": {"ai": 0, "investment": 0, "philosophy": 0, "misc": 0},
    }
    assert client.get("/recent").json() == []
    assert client.get("/search", params={"q": "removed"}).json() == []
    assert client.get("/note/obsidian-1").status_code == 404


def test_stats_works_without_import_state(tmp_path, monkeypatch):
    _write_fixture(tmp_path, monkeypatch)
    (tmp_path / "state.json").unlink()

    response = TestClient(app.app).get("/stats")

    assert response.status_code == 200
    assert response.json()["documents"] == 1
    assert response.json()["categories"]["ai"] == 1


def test_health_reports_capabilities(monkeypatch):
    monkeypatch.setenv("ENABLE_LOCAL_IMPORT", "true")
    monkeypatch.setenv("ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION", "true")
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "false")
    monkeypatch.setenv("KNOWLEDGE_STORE_BACKEND", "filesystem")

    response = TestClient(app.app).get("/health")

    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["knowledge_store"]["backend"] == "filesystem"
    assert payload["capabilities"] == {
        "wiki_read": True,
        "local_import": True,
        "semantic_search": False,
        "rag_chat": False,
    }


def test_import_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_LOCAL_IMPORT", raising=False)
    monkeypatch.delenv("ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION", raising=False)

    response = TestClient(app.app).post("/import")

    assert response.status_code == 503
    assert "ENABLE_LOCAL_IMPORT=true" in response.json()["detail"]


def test_import_requires_publication_opt_in(monkeypatch):
    monkeypatch.setenv("ENABLE_LOCAL_IMPORT", "true")
    monkeypatch.delenv("ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION", raising=False)

    response = TestClient(app.app).post("/import")

    assert response.status_code == 503
    assert "ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION=true" in response.json()["detail"]


def test_import_publication_requires_authenticated_same_origin_request(monkeypatch):
    monkeypatch.setenv("ENABLE_LOCAL_IMPORT", "true")
    monkeypatch.setenv("ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION", "true")
    client = TestClient(app.app)

    response = client.post("/import")

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF protection header is required"

    headers = {"x-csrf-protection": "1", "origin": "http://localhost:5173"}
    response = client.post("/import", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "Document authentication is not configured"

    monkeypatch.setenv("APP_PASSWORD_HASH", "configured-placeholder")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    response = client.post("/import", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_chatgpt_site_cors_is_allowed():
    response = TestClient(app.app).options(
        "/health",
        headers={
            "Origin": "https://ai-wiki-dashboard-minwoo.cheatmin.chatgpt.site",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://ai-wiki-dashboard-minwoo.cheatmin.chatgpt.site"
    )


def test_unknown_cors_origin_is_not_allowed():
    response = TestClient(app.app).options(
        "/health",
        headers={
            "Origin": "https://not-authorized.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_api_import_delegates_to_existing_importer(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("ENABLE_LOCAL_IMPORT", "true")
    monkeypatch.setenv("ENABLE_LEGACY_PUBLIC_WIKI_PUBLICATION", "true")
    monkeypatch.setattr(app, "_require_same_origin", lambda request: None)
    monkeypatch.setattr(app, "_require_document_session", lambda request: None)
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
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "true")
    monkeypatch.setattr(
        app,
        "semantic_search",
        lambda query, db_path, limit: [SemanticResult("Note", 0.9, "snippet", "source-1")],
    )

    response = TestClient(app.app).get("/semantic-search", params={"q": "meaning"})

    assert response.status_code == 200
    assert response.json() == [
        {"title": "Note", "score": 0.9, "snippet": "snippet", "id": "source-1"}
    ]


def test_api_chat_uses_top_semantic_context_and_returns_sources(monkeypatch):
    monkeypatch.setenv("ENABLE_LOCAL_RAG", "true")
    matches = [
        SemanticResult(f"Note {index}", 1 - index / 10, f"snippet {index}", f"source-{index}")
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


def test_rag_endpoints_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_LOCAL_RAG", raising=False)

    semantic_response = TestClient(app.app).get("/semantic-search", params={"q": "meaning"})
    chat_response = TestClient(app.app).post("/chat", json={"question": "meaning"})

    assert semantic_response.status_code == 503
    assert chat_response.status_code == 503
    assert "ENABLE_LOCAL_RAG=true" in semantic_response.json()["detail"]
