from __future__ import annotations

import hashlib

import pytest

from engine.knowledge_reconciliation import (
    Classification,
    RevisionInventory,
    classify_revision,
    classify_write_intent,
)
from engine.postgres_operational import (
    DOCUMENT_ID_RE,
    REVISION_ID_RE,
    InvalidOperationalInput,
    WriteRequest,
    normalize_tag,
    object_keys,
    transition_intent,
    transition_job,
    validate_request,
)


def make_request(**overrides):
    values = {
        "idempotency_key": "op-1",
        "document_id": "a" * 64,
        "expected_current_revision_id": None,
        "source_bytes": b"markdown",
        "metadata": {
            "title": "Title",
            "category": "ai",
            "tags": [" Alpha "],
            "visibility": "private",
        },
    }
    values.update(overrides)
    return WriteRequest(**values)


def test_request_digest_stable_for_mapping_order_and_bound_to_source():
    left = make_request(
        metadata={"title": "Title", "category": "ai", "tags": ["Alpha"], "visibility": "private"}
    )
    right = make_request(
        metadata={"visibility": "private", "tags": ["Alpha"], "category": "ai", "title": "Title"}
    )
    digest, _ = validate_request(left)
    assert digest == validate_request(right)[0]
    assert digest != validate_request(make_request(source_bytes=b"different"))[0]
    assert len(digest) == 64


def test_identifiers_follow_phase_one_contract_and_keys_are_isolated():
    request = make_request()
    assert DOCUMENT_ID_RE.fullmatch(request.document_id)
    digest, _ = validate_request(request)
    revision = "b" * 64
    assert REVISION_ID_RE.fullmatch(revision)
    assert object_keys(request.document_id, revision) == (
        f"knowledge/{request.document_id}/revisions/{revision}/source.md",
        f"knowledge/{request.document_id}/revisions/{revision}/manifest.json",
    )
    assert (
        digest
        == hashlib.sha256(
            __import__("json")
            .dumps(
                {
                    "document_id": request.document_id,
                    "expected_current_revision_id": None,
                    "content_hash": hashlib.sha256(b"markdown").hexdigest(),
                    "metadata": {
                        "title": "Title",
                        "category": "ai",
                        "tags": ["Alpha"],
                        "visibility": "private",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            .encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "input_request",
    [
        make_request(document_id="../unsafe"),
        make_request(expected_current_revision_id="not-a-revision"),
        make_request(metadata={"title": "", "category": "ai", "tags": [], "visibility": "private"}),
        make_request(
            metadata={"title": "T", "category": "unknown", "tags": [], "visibility": "private"}
        ),
        make_request(
            metadata={"title": "T", "category": "ai", "tags": ["AI", "ai"], "visibility": "private"}
        ),
    ],
)
def test_invalid_request_rejected(input_request):
    with pytest.raises(InvalidOperationalInput):
        validate_request(input_request)


def test_processing_transitions_are_explicit():
    assert transition_job("QUEUED", "RUNNING") == "RUNNING"
    assert transition_job("RUNNING", "RETRY_WAIT") == "RETRY_WAIT"
    assert transition_job("RETRY_WAIT", "FAILED") == "FAILED"
    with pytest.raises(InvalidOperationalInput):
        transition_job("SUCCEEDED", "RUNNING")
    assert transition_intent("PREPARED", "SOURCE_WRITTEN") == "SOURCE_WRITTEN"
    assert transition_intent("COMPLETE", "COMMITTED") == "COMMITTED"
    with pytest.raises(InvalidOperationalInput):
        transition_intent("COMMITTED", "PREPARED")
    assert transition_intent("PREPARED", "SOURCE_WRITTEN") == "SOURCE_WRITTEN"
    assert transition_intent("COMPLETE", "COMMITTED") == "COMMITTED"
    with pytest.raises(InvalidOperationalInput):
        transition_intent("COMMITTED", "PREPARED")


def test_reconciliation_classifies_without_deleting():
    assert classify_revision(RevisionInventory(True, True, True, False)) == Classification.HEALTHY
    assert (
        classify_revision(RevisionInventory(True, False, False, True)) == Classification.AUTO_REPAIR
    )
    assert (
        classify_revision(RevisionInventory(True, False, False, False)) == Classification.QUARANTINE
    )
    assert classify_revision(RevisionInventory(False, True, False, False)) == Classification.MANUAL
    assert (
        classify_revision(RevisionInventory(True, True, False, False)) == Classification.QUARANTINE
    )
    assert (
        classify_revision(RevisionInventory(False, False, False, False))
        == Classification.GC_CANDIDATE
    )
    assert (
        classify_revision(RevisionInventory(True, True, True, False, current_pointer_valid=False))
        == Classification.MANUAL
    )
    assert classify_write_intent("PREPARED", 60, 3600) == Classification.AUTO_REPAIR
    assert classify_write_intent("PREPARED", 7200, 3600) == Classification.MANUAL


def test_tag_normalization_is_unicode_aware():
    assert normalize_tag("  ＡＩ ") == "ai"
