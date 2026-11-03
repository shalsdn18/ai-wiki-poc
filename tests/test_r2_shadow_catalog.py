from __future__ import annotations

import hashlib
import io
import json

import pytest

from engine.r2_shadow_catalog import GuardedR2Client, scan_r2


class FakeR2:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(("LIST", kwargs))
        keys = sorted(k for k in self.objects if k.startswith(kwargs["Prefix"]))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, **kwargs):
        self.calls.append(("GET", kwargs))
        if kwargs["Key"] not in self.objects:
            raise KeyError("missing")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def _pair(
    doc,
    rev,
    title="Example",
    updated="2026-01-02T00:00:00Z",
    *,
    hash_override=None,
    manifest_doc=None,
    manifest_rev=None,
    object_override=None,
    created="2026-01-01T00:00:00Z",
):
    source_key = f"knowledge/{doc}/revisions/{rev}/source.md"
    manifest_key = f"knowledge/{doc}/revisions/{rev}/manifest.json"
    source = (
        f"---\ndocument_id: {doc}\ntitle: {title}\ncategory: ai\ntags: [storage]\n"
        "created: '2026-01-01'\nupdated: '2026-01-02'\nvisibility: private\n---\n\nbody\n"
    ).encode()
    body_hash = hash_override or hashlib.sha256(source).hexdigest()
    manifest = {
        "schema_version": 1,
        "document_id": manifest_doc or doc,
        "revision_id": manifest_rev or rev,
        "title": title,
        "category": "ai",
        "tags": ["storage"],
        "visibility": "private",
        "source_url": None,
        "mime_type": "text/markdown; charset=utf-8",
        "content_hash": body_hash,
        "object_key": object_override or source_key,
        "created_at": created,
        "updated_at": updated,
    }
    return source_key, manifest_key, source, json.dumps(manifest).encode()


def _inventory(objects):
    return scan_r2(FakeR2(objects), "bucket")


def test_valid_pair_and_unambiguous_current():
    doc, rev = "a" * 32, "1" * 64
    sk, mk, source, manifest = _pair(doc, rev)
    inventory = _inventory({sk: source, mk: manifest})
    assert inventory.counts["VALID_COMPLETE"] == 1
    assert inventory.documents[0].current_classification == "CURRENT_UNAMBIGUOUS"


def test_malformed_key_is_other_invalid():
    inventory = _inventory({"knowledge/orphan": b"x"})
    assert inventory.counts["OTHER_INVALID"] == 1


def test_source_only_manifest_only_and_invalid_classifications():
    doc = "a" * 32
    cases = []
    rev = "1" * 64
    sk, mk, source, manifest = _pair(doc, rev)
    cases += [({sk: source}, "SOURCE_ONLY"), ({mk: manifest}, "MANIFEST_ONLY")]
    cases.append(({mk: b"{"}, "MALFORMED_MANIFEST"))
    rev = "2" * 64
    sk, mk, source, manifest = _pair(doc, rev)
    cases.append(({sk: source, mk: b"{"}, "MALFORMED_MANIFEST"))
    rev = "3" * 64
    sk, mk, source, manifest = _pair(doc, rev, hash_override="0" * 64)
    cases.append(({sk: source, mk: manifest}, "HASH_MISMATCH"))
    rev = "4" * 64
    sk, mk, source, manifest = _pair(doc, rev, manifest_doc="b" * 32)
    cases.append(({sk: source, mk: manifest}, "ID_MISMATCH"))
    rev = "5" * 64
    sk, mk, source, manifest = _pair(doc, rev, object_override="knowledge/wrong/source.md")
    cases.append(({sk: source, mk: manifest}, "KEY_MISMATCH"))
    rev = "6" * 64
    sk, mk, source, manifest = _pair(doc, rev, updated="no-time")
    cases.append(({sk: source, mk: manifest}, "INVALID_TIMESTAMP"))
    for objects, expected in cases:
        inventory = _inventory(objects)
        assert inventory.counts[expected] == 1


def test_duplicate_effective_timestamp_is_ambiguous_and_lexical():
    doc = "a" * 32
    objects = {}
    for rev in ("1" * 64, "2" * 64):
        sk, mk, source, manifest = _pair(doc, rev)
        objects.update({sk: source, mk: manifest})
    analysis = _inventory(objects).documents[0]
    assert analysis.current_classification == "CURRENT_AMBIGUOUS"
    assert analysis.timestamp_tie and analysis.lexical_fallback
    assert analysis.candidate_revision_id == "2" * 64


def test_broken_selected_source_is_current_invalid_and_bad_manifest_is_ignored():
    doc = "a" * 32
    older, newer = "1" * 64, "2" * 64
    sk1, mk1, source1, manifest1 = _pair(doc, older, updated="2026-01-01T00:00:00Z")
    sk2, mk2, source2, manifest2 = _pair(doc, newer, hash_override="0" * 64)
    blocked = _inventory({sk1: source1, mk1: manifest1, sk2: source2, mk2: manifest2})
    assert blocked.documents[0].current_classification == "CURRENT_INVALID"
    malformed = b"{"
    skipped = _inventory({sk1: source1, mk1: manifest1, sk2: source2, mk2: malformed})
    assert skipped.documents[0].current_classification == "CURRENT_UNAMBIGUOUS"
    assert skipped.documents[0].candidate_revision_id == older


def test_guard_exposes_only_list_and_get_and_rejects_other_prefix():
    raw = FakeR2({})
    guarded = GuardedR2Client(raw, "bucket")
    guarded.list_objects_v2(Bucket="bucket", Prefix="knowledge/")
    with pytest.raises(PermissionError):
        guarded.list_objects_v2(Bucket="bucket", Prefix="documents/")
    with pytest.raises(PermissionError):
        guarded.get_object(Bucket="bucket", Key="documents/file.md")
    assert not hasattr(guarded, "put_object")
    assert not hasattr(guarded, "delete_object")
    assert [call[0] for call in raw.calls] == ["LIST"]
