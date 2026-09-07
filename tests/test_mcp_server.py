from mcp_server import server


def test_mcp_tools_delegate_to_existing_api(monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"path": path}

    monkeypatch.setattr(server, "_request", fake_request)

    server.search_notes("retrieval")
    server.ask_wiki("What is retrieval?")
    server.recent_notes(3)
    server.stats()
    server.import_now()

    assert calls == [
        ("GET", "/semantic-search", {"params": {"q": "retrieval"}}),
        ("POST", "/chat", {"json": {"question": "What is retrieval?"}}),
        ("GET", "/recent", {"params": {"limit": 3}}),
        ("GET", "/stats", {}),
        ("POST", "/import", {}),
    ]


def test_mcp_api_url_is_configurable(monkeypatch):
    monkeypatch.setenv("AI_WIKI_API_URL", "http://api:8000/")
    assert server._api_url() == "http://api:8000"
