import json

from engine import vector_store


def test_sync_indexes_changed_documents_and_deletes_removed(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Note\nBody", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "documents": {
                    "note.md": {
                        "source_id": "source-1",
                        "relative_path": "note.md",
                        "content_hash": "hash-1",
                        "primary_category": "misc",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        vector_store, "embed", lambda text, model=None: calls.append(text) or [1.0, 0.0]
    )
    db = tmp_path / "vectors.db"

    assert vector_store.sync_vault_vectors(vault, state, db) == 1
    assert vector_store.sync_vault_vectors(vault, state, db) == 0
    assert len(calls) == 1
    note.unlink()
    assert vector_store.sync_vault_vectors(vault, state, db) == 0
    with vector_store._connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM document_vectors").fetchone()[0] == 0


def test_semantic_search_returns_ranked_result(tmp_path, monkeypatch):
    db = tmp_path / "vectors.db"
    with vector_store._connect(db) as connection:
        connection.execute(
            "INSERT INTO document_vectors VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("source-1", "hash", "Note", "note.md", "Relevant content", "[1.0, 0.0]", "bge-m3"),
        )
    monkeypatch.setattr(vector_store, "embed", lambda text, model=None: [1.0, 0.0])

    result = vector_store.semantic_search("query", db)
    assert result[0].id == "source-1"
    assert result[0].score == 1.0
