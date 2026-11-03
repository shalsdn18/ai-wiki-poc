import io

import pytest

from engine.r2_revision_objects import (
    R2RevisionObjects,
    RevisionObjectConflict,
    RevisionObjectUnavailable,
)


class ConditionalFailure(Exception):
    def __init__(self):
        self.response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }
        super().__init__("conditional write rejected")


class FakeR2:
    supports_if_none_match = True

    def __init__(self):
        self.objects = {}
        self.put_kwargs = []

    def put_object(self, **kwargs):
        self.put_kwargs.append(kwargs)
        assert kwargs["IfNoneMatch"] == "*"
        key = kwargs["Key"]
        if key in self.objects:
            raise ConditionalFailure()
        self.objects[key] = kwargs["Body"]

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def _key():
    return f"knowledge/{'a' * 64}/revisions/{'b' * 64}/source.md"


def test_put_is_atomic_create_only_and_exact_retry_is_idempotent():
    client = FakeR2()
    objects = R2RevisionObjects(client, "test-bucket")
    assert objects.put_if_absent(_key(), b"source", "text/markdown; charset=utf-8") == b"source"
    assert objects.put_if_absent(_key(), b"source", "text/markdown; charset=utf-8") == b"source"
    assert len(client.put_kwargs) == 2
    assert client.objects[_key()] == b"source"


def test_put_rejects_different_bytes_at_existing_immutable_key():
    client = FakeR2()
    objects = R2RevisionObjects(client, "test-bucket")
    objects.put_if_absent(_key(), b"source", "text/markdown; charset=utf-8")
    with pytest.raises(RevisionObjectConflict):
        objects.put_if_absent(_key(), b"changed", "text/markdown; charset=utf-8")
    assert client.objects[_key()] == b"source"


def test_put_fails_closed_without_conditional_write_support():
    class Unsupported:
        def put_object(self, **kwargs):
            raise AssertionError("must not perform an unconditional write")

        def get_object(self, **kwargs):
            raise AssertionError("must not perform a read before capability validation")

    with pytest.raises(RevisionObjectUnavailable):
        R2RevisionObjects(Unsupported(), "test-bucket")


def test_only_knowledge_revision_keys_are_accepted():
    objects = R2RevisionObjects(FakeR2(), "test-bucket")
    with pytest.raises(RevisionObjectUnavailable):
        objects.put_if_absent("documents/private.md", b"secret", "text/markdown")


def test_manifest_json_content_type_is_accepted_with_exact_key_layout():
    client = FakeR2()
    objects = R2RevisionObjects(client, "test-bucket")
    key = f"knowledge/{'a' * 64}/revisions/{'b' * 64}/manifest.json"
    assert objects.put_if_absent(key, b"{}", "application/json; charset=utf-8") == b"{}"


def test_source_markdown_serialization_is_stable_across_jsonb_key_order():
    from engine.knowledge_writer import _source_bytes

    metadata = {
        "title": "Stable",
        "category": "ai",
        "tags": ["hash"],
        "visibility": "private",
        "source_url": None,
        "created": "2026-01-01",
        "updated": "2026-01-02",
    }
    reordered = dict(reversed(list(metadata.items())))
    assert _source_bytes("a" * 64, metadata, "body") == _source_bytes("a" * 64, reordered, "body")
