"""Cloudflare R2 implementation of the Markdown document store protocol."""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

try:
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # Allows fully offline fake-client tests without boto3 installed.
    Config = None

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        def __init__(self, response, operation_name=""):
            self.response = response
            super().__init__(operation_name or "S3 client error")

from engine import document_store


class R2Store:
    def __init__(self, client: Any, bucket_name: str):
        self.client = client
        self.bucket_name = bucket_name

    @classmethod
    def from_environment(cls) -> "R2Store":
        endpoint_url = os.getenv("R2_ENDPOINT_URL", "").strip()
        account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
        bucket_name = os.getenv("R2_BUCKET_NAME", "").strip()
        access_key_id = os.getenv("R2_ACCESS_KEY_ID", "").strip()
        secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
        if not endpoint_url and account_id:
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        if not all((endpoint_url, bucket_name, access_key_id, secret_access_key)):
            raise document_store.DocumentStorageConfigurationError(
                "R2 storage requires endpoint, bucket, access key, and secret key settings"
            )

        try:
            if Config is None:
                raise ImportError("boto3 and botocore are required for R2 storage")
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name="auto",
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(
                    connect_timeout=5,
                    read_timeout=20,
                    retries={"max_attempts": 2, "mode": "standard"},
                    s3={"addressing_style": "path"},
                ),
            )
        except (BotoCoreError, OSError, ValueError, ImportError) as exc:
            raise document_store.DocumentStorageUnavailableError(
                "Could not initialize the R2 document store"
            ) from exc
        return cls(client, bucket_name)

    @staticmethod
    def _object_key(document_id: str) -> str:
        if not document_store._ID_PATTERN.fullmatch(document_id):
            raise document_store.InvalidDocumentIdError(
                "Document ID must be a 32-character lowercase UUID hex value"
            )
        return f"documents/{document_id}.md"

    def _client_error(self, exc: Exception, operation: str) -> None:
        code = "storage error"
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", code))
        raise document_store.DocumentStorageUnavailableError(
            f"R2 document {operation} failed ({code})"
        ) from exc

    def _read(self, key: str) -> dict[str, Any]:
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            body = response["Body"]
            try:
                raw_bytes = body.read()
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise document_store.DocumentNotFoundError(key) from exc
            self._client_error(exc, "read")
        except (BotoCoreError, OSError, TimeoutError, KeyError) as exc:
            self._client_error(exc, "read")

        try:
            raw = raw_bytes.decode("utf-8")
        except (AttributeError, UnicodeDecodeError) as exc:
            raise document_store.DocumentStorageError(
                f"R2 Markdown object is not valid UTF-8: {key}"
            ) from exc
        document_id = key.rsplit("/", 1)[-1][:-3]
        return document_store._parse_markdown(raw, document_id, key)

    def _write(self, document_id: str, metadata: dict[str, Any], content: str) -> None:
        key = self._object_key(document_id)
        body = document_store._serialize(metadata, content).encode("utf-8")
        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=body,
                ContentType="text/markdown; charset=utf-8",
            )
        except (ClientError, BotoCoreError, OSError, TimeoutError) as exc:
            self._client_error(exc, "write")

    def list_documents(self) -> list[dict[str, Any]]:
        documents = []
        continuation_token = None
        try:
            while True:
                params = {"Bucket": self.bucket_name, "Prefix": "documents/"}
                if continuation_token:
                    params["ContinuationToken"] = continuation_token
                page = self.client.list_objects_v2(**params)
                for entry in page.get("Contents", []):
                    key = entry.get("Key", "")
                    filename = key.removeprefix("documents/")
                    if not filename.endswith(".md"):
                        continue
                    document_id = filename[:-3]
                    if not document_store._ID_PATTERN.fullmatch(document_id):
                        continue
                    documents.append(self._read(key))
                if not page.get("IsTruncated"):
                    break
                continuation_token = page.get("NextContinuationToken")
                if not continuation_token:
                    raise document_store.DocumentStorageUnavailableError(
                        "R2 document listing returned no continuation token"
                    )
        except (ClientError, BotoCoreError, OSError, TimeoutError) as exc:
            self._client_error(exc, "list")
        return sorted(
            documents,
            key=lambda document: (document["updated_at"], document["id"]),
            reverse=True,
        )

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self._read(self._object_key(document_id))

    def create_document(
        self, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]:
        document_id = uuid4().hex
        now = document_store._utc_now()
        metadata = {
            "id": document_id,
            "title": title,
            "tags": tags or [],
            "content_hash": document_store._content_hash(content),
            "created_at": now,
            "updated_at": now,
        }
        self._write(document_id, metadata, content)
        key = self._object_key(document_id)
        return document_store._parse_markdown(
            document_store._serialize(metadata, content), document_id, key
        )

    def update_document(
        self, document_id: str, title: str, content: str, tags: list[str] | None = None
    ) -> dict[str, Any]:
        current = self.get_document(document_id)
        metadata = {
            "id": document_id,
            "title": title,
            "tags": tags or [],
            "content_hash": document_store._content_hash(content),
            "created_at": current["created_at"],
            "updated_at": document_store._utc_now(),
        }
        self._write(document_id, metadata, content)
        key = self._object_key(document_id)
        return document_store._parse_markdown(
            document_store._serialize(metadata, content), document_id, key
        )

    def delete_document(self, document_id: str) -> None:
        key = self._object_key(document_id)
        self._read(key)
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
        except (ClientError, BotoCoreError, OSError, TimeoutError) as exc:
            self._client_error(exc, "delete")
