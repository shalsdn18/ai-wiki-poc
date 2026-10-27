import base64

import pytest

from engine.github_knowledge_store import (
    GitHubAppInstallationCredentialProvider,
    GitHubKnowledgeStore,
)
from engine.knowledge_store import (
    FilesystemKnowledgeStore,
    KnowledgeUnavailableError,
)
from engine.publish_policy import select_explicitly_published

SHA_A = "a" * 40
SHA_B = "b" * 40


def note(title, visibility="private", category="ai", body="Source content", extra=""):
    return (
        "---\n"
        f"title: {title}\n"
        f"category: {category}\n"
        "tags: [sample]\n"
        'created: "2025-01-01"\n'
        'updated: "2026-01-01"\n'
        f"visibility: {visibility}\n"
        f"{extra}"
        "---\n\n"
        f"{body}\n"
    )


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeGitHubTransport:
    def __init__(self, files=None, commit=SHA_A, wrap_base64=False):
        self.files = files or {"knowledge/ai/nested/example.md": note("Example")}
        self.commit = commit
        self.calls = []
        self.fail_blob_sha = None
        self.fail_commit = False
        self.token_seen = []
        self.wrap_base64 = wrap_base64

    def request_json(self, method, url, headers, body=None):
        self.calls.append((method, url, headers.copy(), body))
        authorization = headers.get("Authorization", "")
        self.token_seen.append(authorization)
        if "/commits/" in url:
            if self.fail_commit:
                raise RuntimeError("GitHub says token=never-log-this")
            return {"sha": self.commit}
        if "/git/trees/" in url:
            entries = []
            for index, path in enumerate(self.files):
                entries.append({"path": path, "type": "blob", "sha": f"blob-{index}"})
            entries.extend(
                [
                    {"path": "README.md", "type": "blob", "sha": "ignored"},
                    {"path": "knowledge/ai/subdir", "type": "tree", "sha": "tree"},
                    {"path": "knowledge/ai/no.md", "type": "blob"},
                ]
            )
            return {"truncated": False, "tree": entries}
        if "/git/blobs/" in url:
            blob_sha = url.rsplit("/", 1)[-1]
            if blob_sha == self.fail_blob_sha:
                raise RuntimeError("private transport detail")
            index = int(blob_sha.removeprefix("blob-"))
            raw = list(self.files.values())[index]
            encoder = base64.encodebytes if self.wrap_base64 else base64.b64encode
            return {"encoding": "base64", "content": encoder(raw.encode()).decode()}
        raise AssertionError(f"Unexpected GitHub URL: {url}")


class FakeCredentials:
    def get_token(self):
        return "mock-installation-token"


def make_store(transport, clock=None, **kwargs):
    clock = clock or FakeClock()
    return GitHubKnowledgeStore(
        "owner",
        "private-repo",
        "main",
        credential_provider=FakeCredentials(),
        transport=transport,
        now=clock,
        **kwargs,
    )


def test_github_adapter_recursive_discovery_and_filesystem_parity(tmp_path):
    files = {
        "knowledge/ai/nested/example.md": note("Example", body="Needle in body"),
        "knowledge/philosophy/stoic.md": note("Stoic", category="philosophy", visibility="public"),
    }
    root = tmp_path / "knowledge"
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    github = make_store(FakeGitHubTransport(files, wrap_base64=True))
    filesystem = FilesystemKnowledgeStore(root)

    assert [item.as_dict() for item in github.list_documents()] == [
        item.as_dict() for item in filesystem.list_documents()
    ]
    assert github.search("needle")[0].title == "Example"
    assert github.stats("private")["documents"] == 1
    doc = github.list_documents()[0]
    assert github.get_document(doc.id).id == filesystem.get_document(doc.id).id
    assert github.list_documents("public")[0].title == "Stoic"


def test_same_sha_reuses_snapshot_and_does_not_refetch_tree_or_blobs():
    clock = FakeClock()
    transport = FakeGitHubTransport()
    store = make_store(transport, clock)
    store.list_documents()
    initial_call_count = len(transport.calls)

    clock.advance(31)
    store.list_documents()

    assert len(transport.calls) == initial_call_count + 1
    assert "/commits/main" in transport.calls[-1][1]
    assert store.health_status()["status"] == "HEALTHY"
    assert store.health_status()["commit"] == SHA_A[:12]


def test_changed_sha_refreshes_complete_snapshot_atomically_and_serves_stale_on_failure():
    clock = FakeClock()
    transport = FakeGitHubTransport()
    store = make_store(transport, clock, ref_check_seconds=10, stale_max_seconds=100)
    original = store.list_documents()[0]

    transport.commit = SHA_B
    transport.files = {"knowledge/investment/new.md": note("New", category="investment")}
    transport.fail_blob_sha = "blob-0"
    clock.advance(11)
    stale = store.list_documents()

    assert [item.title for item in stale] == [original.title]
    assert store.health_status()["status"] == "STALE"
    assert store.health_status()["commit"] == SHA_A[:12]

    transport.fail_blob_sha = None
    clock.advance(11)
    refreshed = store.list_documents()
    assert [item.title for item in refreshed] == ["New"]
    assert store.health_status()["status"] == "HEALTHY"
    assert store.health_status()["commit"] == SHA_B[:12]


def test_partial_fetch_without_previous_snapshot_is_unavailable():
    transport = FakeGitHubTransport()
    transport.fail_commit = True
    store = make_store(transport)

    with pytest.raises(KnowledgeUnavailableError, match="unavailable") as error:
        store.list_documents()

    assert "never-log-this" not in str(error.value)
    assert store.health_status()["status"] == "UNAVAILABLE"


def test_stale_snapshot_expires_to_unavailable():
    clock = FakeClock()
    transport = FakeGitHubTransport()
    store = make_store(transport, clock, ref_check_seconds=0, stale_max_seconds=10)
    store.list_documents()
    transport.fail_commit = True
    clock.advance(1)
    assert store.list_documents()[0].title == "Example"
    assert store.health_status()["status"] == "STALE"
    clock.advance(11)

    with pytest.raises(KnowledgeUnavailableError):
        store.list_documents()
    assert store.health_status()["status"] == "UNAVAILABLE"


def test_malformed_markdown_is_isolated_and_health_has_no_document_metadata():
    files = {
        "knowledge/ai/good.md": note("Good"),
        "knowledge/ai/bad.md": "---\ntitle: Missing fields\n---\nbody",
    }
    store = make_store(FakeGitHubTransport(files))

    assert [item.title for item in store.list_documents()] == ["Good"]
    status = store.health_status()
    assert status["malformed_documents"] == 1
    assert "Good" not in str(status)
    assert "knowledge/ai" not in str(status)


def test_github_requests_use_mocked_private_auth_and_redact_transport_errors():
    transport = FakeGitHubTransport()
    store = make_store(transport)
    store.list_documents()

    assert all(
        authorization == "Bearer mock-installation-token" for authorization in transport.token_seen
    )

    transport.fail_commit = True
    with pytest.raises(KnowledgeUnavailableError) as error:
        store._resolve_commit()
    assert "mock-installation-token" not in str(error.value)
    assert "never-log-this" not in str(error.value)


def test_github_app_provider_mints_and_caches_read_only_installation_token():
    class TokenTransport:
        def __init__(self):
            self.calls = []

        def request_json(self, method, url, headers, body=None):
            self.calls.append((method, url, headers, body))
            return {"token": "short-lived", "expires_at": "2026-10-27T02:00:00Z"}

    clock = FakeClock()
    transport = TokenTransport()
    provider = GitHubAppInstallationCredentialProvider(
        "123", "456", "private key", transport=transport, now=clock
    )
    provider._app_jwt = lambda: "signed-app-jwt"

    assert provider.get_token() == "short-lived"
    assert provider.get_token() == "short-lived"
    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/app/installations/456/access_tokens")
    assert headers["Authorization"] == "Bearer signed-app-jwt"
    assert body == b'{"permissions":{"contents":"read"}}'


def test_github_app_jwt_is_signed_and_contains_limited_lifetime_claims():
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoded_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    provider = GitHubAppInstallationCredentialProvider("app-123", "install-456", encoded_key)

    claims = jwt.decode(
        provider._app_jwt(),
        private_key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False},
    )

    assert claims["iss"] == "app-123"
    assert 500 <= claims["exp"] - claims["iat"] <= 600


def test_publish_policy_requires_visibility_and_explicit_selection():
    private = make_store(FakeGitHubTransport()).list_documents()[0]
    public_store = make_store(
        FakeGitHubTransport({"knowledge/ai/public.md": note("Public", "public")})
    )
    public = public_store.list_documents()[0]

    assert select_explicitly_published([private, public], set()) == []
    assert select_explicitly_published([private, public], {private.id}) == []
    assert select_explicitly_published([private, public], {public.id}) == [public]
