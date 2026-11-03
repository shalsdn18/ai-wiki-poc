"""Create-only access to canonical revision objects under the Knowledge prefix."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol


class RevisionObjectError(RuntimeError):
    """Base class for sanitized R2 revision object errors."""


class RevisionObjectConflict(RevisionObjectError):
    """An immutable object key already contains different bytes."""


class RevisionObjectUnavailable(RevisionObjectError):
    """R2 could not complete or verify an object operation."""


class RevisionS3Client(Protocol):
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


class R2RevisionObjects:
    """Read and create immutable objects; never list or delete bucket objects."""

    def __init__(self, client: RevisionS3Client, bucket: str, prefix: str = "knowledge"):
        self.client = client
        self.bucket = bucket.strip()
        self.prefix = prefix.strip("/")
        if not self.bucket or self.prefix != "knowledge":
            raise RevisionObjectUnavailable("Canonical Knowledge R2 configuration is invalid")
        try:
            model = client.meta.service_model.operation_model("PutObject")
            if "IfNoneMatch" not in model.input_shape.members:
                raise RevisionObjectUnavailable(
                    "R2 client does not support conditional create-only writes"
                )
        except AttributeError:
            # Lightweight test doubles can explicitly model conditional put behavior.
            if not getattr(client, "supports_if_none_match", False):
                raise RevisionObjectUnavailable(
                    "R2 client does not support conditional create-only writes"
                ) from None

    def _validate_key(self, key: str) -> None:
        parts = key.split("/")
        if (
            len(parts) != 5
            or parts[0] != "knowledge"
            or not re.fullmatch(r"[a-f0-9]{32,64}", parts[1])
            or parts[2] != "revisions"
            or not re.fullmatch(r"[a-f0-9]{64}", parts[3])
            or parts[4] not in {"source.md", "manifest.json"}
        ):
            raise RevisionObjectUnavailable("R2 revision key is outside the canonical layout")

    @staticmethod
    def _read_body(response: dict[str, Any]) -> bytes:
        body = response.get("Body")
        if body is None:
            raise RevisionObjectUnavailable("R2 revision object could not be read")
        try:
            raw = body.read()
            if not isinstance(raw, bytes):
                raise RevisionObjectUnavailable("R2 revision object could not be read")
            return raw
        except RevisionObjectUnavailable:
            raise
        except Exception:  # noqa: BLE001 - SDK errors may contain key and account data
            raise RevisionObjectUnavailable("R2 revision object could not be read") from None
        finally:
            close = getattr(body, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001, S110 - cleanup must not mask the read result
                    pass

    def get(self, key: str) -> bytes:
        self._validate_key(key)
        try:
            return self._read_body(self.client.get_object(Bucket=self.bucket, Key=key))
        except RevisionObjectError:
            raise
        except Exception:  # noqa: BLE001 - do not expose SDK response details
            raise RevisionObjectUnavailable("R2 revision object is unavailable") from None

    @staticmethod
    def _is_precondition_failure(exc: Exception) -> bool:
        response = getattr(exc, "response", {})
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        return (
            error.get("Code") in {"PreconditionFailed", "412", "ConditionalRequestConflict"}
            or metadata.get("HTTPStatusCode") == 412
        )

    def put_if_absent(self, key: str, body: bytes, content_type: str) -> bytes:
        """Use atomic If-None-Match, then read back; never overwrite or delete."""
        self._validate_key(key)
        expected_type = "text/markdown" if key.endswith("/source.md") else "application/json"
        if not isinstance(body, bytes) or not content_type.casefold().startswith(expected_type):
            raise RevisionObjectUnavailable("R2 revision object input is invalid")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except Exception as exc:  # noqa: BLE001 - classify without exposing SDK details
            if self._is_precondition_failure(exc):
                existing = self.get(key)
                if existing != body:
                    raise RevisionObjectConflict(
                        "Immutable R2 key contains bytes from another operation"
                    ) from None
                return existing
            # A timeout can occur after R2 committed the conditional write. Exact read-back
            # resolves that case; a missing read is still inconclusive, so retry later with
            # the same key and If-None-Match rather than attempting a destructive rollback.
            try:
                existing = self.get(key)
            except RevisionObjectUnavailable:
                raise RevisionObjectUnavailable("R2 conditional write outcome is unknown") from None
            if existing == body:
                return existing
            raise RevisionObjectConflict(
                "Immutable R2 key contains bytes from another operation"
            ) from None
        verified = self.get(key)
        if verified != body:
            raise RevisionObjectConflict("R2 read-back bytes do not match the requested object")
        if hashlib.sha256(verified).digest() != hashlib.sha256(body).digest():
            raise RevisionObjectConflict("R2 read-back hash does not match the requested object")
        return verified
