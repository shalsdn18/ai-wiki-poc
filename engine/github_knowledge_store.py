"""Read-only, commit-pinned GitHub adapter for private knowledge repositories."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import jwt

from engine.knowledge_store import (
    CATEGORIES,
    KnowledgeDocument,
    KnowledgeDocumentError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    KnowledgeUnavailableError,
    parse_markdown_document,
    search_documents,
)

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DEFAULT_API_ROOT = "https://api.github.com"
_DEFAULT_REF_CHECK_SECONDS = 30.0
_DEFAULT_STALE_MAX_SECONDS = 900.0


class GitHubCredentialProvider(Protocol):
    def get_token(self) -> str: ...


class JSONTransport(Protocol):
    def request_json(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, Any]: ...


class GitHubAPITransport:
    """Small urllib transport; errors intentionally omit response bodies and headers."""

    def request_json(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, Any]:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
            raise KnowledgeUnavailableError("GitHub knowledge source is unavailable") from None
        if not isinstance(result, dict):
            raise KnowledgeUnavailableError("GitHub knowledge source returned invalid data")
        return result


class GitHubAppInstallationCredentialProvider:
    """Mint and cache short-lived installation tokens from GitHub App secrets."""

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key: str,
        transport: JSONTransport | None = None,
        api_root: str = _DEFAULT_API_ROOT,
        now: Callable[[], float] = time.time,
    ):
        self.app_id = app_id.strip()
        self.installation_id = installation_id.strip()
        self.private_key = private_key
        self.transport = transport or GitHubAPITransport()
        self.api_root = api_root.rstrip("/")
        self.now = now
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, transport: JSONTransport | None = None):
        app_id = os.getenv("KNOWLEDGE_GITHUB_APP_ID", "").strip()
        installation_id = os.getenv("KNOWLEDGE_GITHUB_INSTALLATION_ID", "").strip()
        private_key = os.getenv("KNOWLEDGE_GITHUB_APP_PRIVATE_KEY", "")
        if not (app_id and installation_id and private_key):
            raise KnowledgeUnavailableError("GitHub App credentials are not configured")
        return cls(app_id, installation_id, private_key, transport=transport)

    def _app_jwt(self) -> str:
        try:
            issued_at = int(self.now()) - 30
            return jwt.encode(
                {"iat": issued_at, "exp": issued_at + 540, "iss": self.app_id},
                self.private_key,
                algorithm="RS256",
            )
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
            raise KnowledgeUnavailableError("GitHub App token could not be created") from None

    def get_token(self) -> str:
        with self._lock:
            if self._token and self.now() < self._expires_at - 60:
                return self._token
            url = (
                f"{self.api_root}/app/installations/"
                f"{quote(self.installation_id, safe='')}/access_tokens"
            )
            try:
                payload = self.transport.request_json(
                    "POST",
                    url,
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {self._app_jwt()}",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Content-Type": "application/json",
                        "User-Agent": "ai-wiki-knowledge-reader",
                    },
                    b'{"permissions":{"contents":"read"}}',
                )
                token = payload.get("token")
                expiry = payload.get("expires_at")
                expires_at = datetime.fromisoformat(expiry).timestamp()
                if not isinstance(token, str) or not token.strip() or expires_at <= self.now():
                    raise ValueError("Invalid installation token response")
            except (KnowledgeUnavailableError, ValueError, TypeError, AttributeError, KeyError):
                raise KnowledgeUnavailableError(
                    "GitHub App installation token is unavailable"
                ) from None
            self._token = token
            self._expires_at = expires_at
            return token


@dataclass(frozen=True)
class KnowledgeSnapshot:
    commit_sha: str
    documents: tuple[KnowledgeDocument, ...]
    fetched_at: float
    malformed_count: int


class GitHubKnowledgeStore(KnowledgeStore):
    """Expose a complete immutable Markdown snapshot at one resolved commit."""

    def __init__(
        self,
        owner: str,
        repo: str,
        ref: str,
        knowledge_root: str = "knowledge",
        credential_provider: GitHubCredentialProvider | None = None,
        transport: JSONTransport | None = None,
        api_root: str = _DEFAULT_API_ROOT,
        ref_check_seconds: float = _DEFAULT_REF_CHECK_SECONDS,
        stale_max_seconds: float = _DEFAULT_STALE_MAX_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self.owner = owner.strip()
        self.repo = repo.strip()
        self.ref = ref.strip()
        self.knowledge_root = knowledge_root.strip("/")
        if not all((self.owner, self.repo, self.ref, self.knowledge_root)):
            raise KnowledgeUnavailableError("GitHub knowledge repository is not configured")
        if self.knowledge_root.startswith("/") or ".." in self.knowledge_root.split("/"):
            raise KnowledgeUnavailableError("GitHub knowledge root is invalid")
        self.transport = transport or GitHubAPITransport()
        self.credential_provider = (
            credential_provider
            or GitHubAppInstallationCredentialProvider.from_environment(self.transport)
        )
        self.api_root = api_root.rstrip("/")
        self.ref_check_seconds = max(0.0, ref_check_seconds)
        self.stale_max_seconds = max(0.0, stale_max_seconds)
        self.now = now
        self._snapshot: KnowledgeSnapshot | None = None
        self._last_check = 0.0
        self._last_error_at: float | None = None
        self._status = "UNAVAILABLE"
        self._lock = threading.RLock()

    def _api_request(self, method: str, endpoint: str) -> dict[str, Any]:
        try:
            token = self.credential_provider.get_token()
            url = f"{self.api_root}{endpoint}"
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-wiki-knowledge-reader",
            }
            return self.transport.request_json(method, url, headers)
        except KnowledgeUnavailableError:
            raise
        except Exception:  # noqa: BLE001 - normalize injected transport/provider failures
            raise KnowledgeUnavailableError("GitHub knowledge source is unavailable") from None

    def _resolve_commit(self) -> str:
        owner = quote(self.owner, safe="")
        repo = quote(self.repo, safe="")
        ref = quote(self.ref, safe="")
        payload = self._api_request("GET", f"/repos/{owner}/{repo}/commits/{ref}")
        sha = payload.get("sha")
        if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
            raise KnowledgeUnavailableError("GitHub knowledge ref could not be resolved")
        return sha.lower()

    def _fetch_snapshot(self, commit_sha: str) -> KnowledgeSnapshot:
        owner = quote(self.owner, safe="")
        repo = quote(self.repo, safe="")
        tree = self._api_request("GET", f"/repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1")
        entries = tree.get("tree")
        if tree.get("truncated") or not isinstance(entries, list):
            raise KnowledgeUnavailableError("GitHub knowledge tree is incomplete")

        prefix = f"{self.knowledge_root}/"
        blobs = []
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            sha = entry.get("sha") if isinstance(entry, dict) else None
            if (
                isinstance(path, str)
                and path.startswith(prefix)
                and path.lower().endswith(".md")
                and entry.get("type") == "blob"
                and isinstance(sha, str)
            ):
                relative_path = path[len(prefix) :]
                if relative_path and ".." not in relative_path.split("/"):
                    blobs.append((relative_path, sha))

        documents: list[KnowledgeDocument] = []
        malformed_count = 0
        for relative_path, blob_sha in blobs:
            payload = self._api_request(
                "GET", f"/repos/{owner}/{repo}/git/blobs/{quote(blob_sha, safe='')}"
            )
            if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
                raise KnowledgeUnavailableError("GitHub knowledge document could not be read")
            try:
                encoded_content = "".join(payload["content"].split())
                raw = base64.b64decode(encoded_content, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                raise KnowledgeUnavailableError(
                    "GitHub knowledge document could not be decoded"
                ) from None
            try:
                documents.append(parse_markdown_document(relative_path, raw))
            except KnowledgeDocumentError:
                malformed_count += 1

        documents.sort(key=lambda item: (item.updated, item.id), reverse=True)
        return KnowledgeSnapshot(commit_sha, tuple(documents), self.now(), malformed_count)

    def _current_snapshot(self) -> KnowledgeSnapshot:
        with self._lock:
            now = self.now()
            if self._snapshot is not None and now - self._last_check < self.ref_check_seconds:
                if (
                    self._status == "STALE"
                    and self._last_error_at is not None
                    and now - self._last_error_at > self.stale_max_seconds
                ):
                    self._status = "UNAVAILABLE"
                    raise KnowledgeUnavailableError("GitHub knowledge source is unavailable")
                if self._status == "UNAVAILABLE":
                    raise KnowledgeUnavailableError("GitHub knowledge source is unavailable")
                return self._snapshot

            self._last_check = now
            try:
                commit_sha = self._resolve_commit()
                if self._snapshot is not None and self._snapshot.commit_sha == commit_sha:
                    self._status = "HEALTHY"
                    self._last_error_at = None
                    return self._snapshot
                snapshot = self._fetch_snapshot(commit_sha)
            except KnowledgeUnavailableError:
                self._last_error_at = self.now()
                if (
                    self._snapshot is not None
                    and self._last_error_at - self._snapshot.fetched_at <= self.stale_max_seconds
                ):
                    self._status = "STALE"
                    return self._snapshot
                self._status = "UNAVAILABLE"
                raise KnowledgeUnavailableError("GitHub knowledge source is unavailable") from None

            # Publish only after ref, tree, all blobs, and parsing complete.
            self._snapshot = snapshot
            self._status = "HEALTHY"
            self._last_error_at = None
            return snapshot

    def list_documents(self, visibility: str | None = None) -> list[KnowledgeDocument]:
        if visibility is not None and visibility not in ("private", "public"):
            raise KnowledgeError("Invalid visibility filter")
        documents = self._current_snapshot().documents
        return [
            replace(item, tags=list(item.tags))
            for item in documents
            if visibility is None or item.visibility == visibility
        ]

    def get_document(self, document_id: str) -> KnowledgeDocument:
        for document in self.list_documents():
            if document.id == document_id:
                return document
        raise KnowledgeNotFoundError(document_id)

    def search(self, query: str, limit: int = 50) -> list[KnowledgeDocument]:
        return search_documents(self.list_documents(), query, limit)

    def stats(self, visibility: str | None = None) -> dict[str, Any]:
        counts = {category: 0 for category in CATEGORIES}
        documents = self.list_documents(visibility)
        for document in documents:
            counts[document.category] += 1
        return {"documents": len(documents), "categories": counts}

    def health_status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot
            return {
                "backend": "github",
                "status": self._status,
                "ref": self.ref,
                "commit": snapshot.commit_sha[:12] if snapshot else None,
                "last_refresh": (
                    datetime.fromtimestamp(snapshot.fetched_at, UTC).isoformat()
                    if snapshot
                    else None
                ),
                "malformed_documents": snapshot.malformed_count if snapshot else 0,
            }


@lru_cache(maxsize=16)
def _configured_github_store(
    owner: str, repo: str, ref: str, root: str, ref_check_seconds: float, stale_max_seconds: float
) -> GitHubKnowledgeStore:
    return GitHubKnowledgeStore(
        owner,
        repo,
        ref,
        root,
        ref_check_seconds=ref_check_seconds,
        stale_max_seconds=stale_max_seconds,
    )


def get_github_store() -> GitHubKnowledgeStore:
    owner = os.getenv("KNOWLEDGE_GITHUB_OWNER", "").strip()
    repo = os.getenv("KNOWLEDGE_GITHUB_REPO", "").strip()
    ref = os.getenv("KNOWLEDGE_GITHUB_REF", "main").strip()
    root = os.getenv("KNOWLEDGE_GITHUB_ROOT", "knowledge").strip("/")
    try:
        ref_check_seconds = max(0.0, float(os.getenv("KNOWLEDGE_GITHUB_REF_CHECK_SECONDS", "30")))
        stale_max_seconds = max(0.0, float(os.getenv("KNOWLEDGE_GITHUB_STALE_MAX_SECONDS", "900")))
    except ValueError:
        raise KnowledgeUnavailableError("GitHub knowledge cache settings are invalid") from None
    return _configured_github_store(owner, repo, ref, root, ref_check_seconds, stale_max_seconds)
