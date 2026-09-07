import sqlite3

from engine.cache import get_cached_payload, save_cached_payload
from engine.schemas import InboxKnowledgePayload


def _payload(title="Cached"):
    return InboxKnowledgePayload(
        title=title,
        summary="Cached summary",
        primary_category="misc",
        categories=["misc"],
        importance=3,
        topic=title,
    )


def test_cache_miss_and_hit_create_sqlite_database(tmp_path):
    cache_path = tmp_path / "nested" / "cache.db"

    assert get_cached_payload("hash-1", "model-a", cache_path) is None
    save_cached_payload("hash-1", "model-a", _payload(), cache_path)

    assert cache_path.exists()
    assert get_cached_payload("hash-1", "model-a", cache_path).title == "Cached"


def test_cache_model_change_is_a_miss(tmp_path):
    cache_path = tmp_path / "cache.db"
    save_cached_payload("hash-1", "model-a", _payload(), cache_path)

    assert get_cached_payload("hash-1", "model-b", cache_path) is None


def test_corrupted_cache_entry_is_ignored(tmp_path):
    cache_path = tmp_path / "cache.db"
    save_cached_payload("hash-1", "model-a", _payload(), cache_path)
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE llm_cache SET payload_json = ? WHERE content_hash = ?",
            ("not-json", "hash-1"),
        )

    assert get_cached_payload("hash-1", "model-a", cache_path) is None
