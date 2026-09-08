from types import SimpleNamespace

from engine import watcher


def _handler(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir(exist_ok=True)
    return watcher.ObsidianEventHandler(vault, tmp_path / "wiki", tmp_path / "state", 1)


def test_watcher_filters_non_markdown_and_generated_paths(tmp_path):
    vault = tmp_path / "Vault"
    assert watcher._should_watch(vault / "note.md", vault)
    assert not watcher._should_watch(vault / "wiki" / "index.md", vault)
    assert not watcher._should_watch(vault / ".git" / "config.md", vault)
    assert not watcher._should_watch(vault / ".obsidian" / "app.md", vault)
    assert not watcher._should_watch(vault / ".obsidian_import_state.json", vault)
    assert not watcher._should_watch(vault / "note.txt", vault)


def test_watcher_debounces_markdown_events_and_imports_once(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nContent", encoding="utf-8")
    timers = []
    imports = []

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(watcher.threading, "Timer", FakeTimer)
    monkeypatch.setattr(
        watcher, "run_pipeline", lambda *args, **kwargs: imports.append((args, kwargs))
    )
    handler = watcher.ObsidianEventHandler(vault, tmp_path / "wiki", tmp_path / "state", 1)
    handler.start()
    event = SimpleNamespace(is_directory=False, src_path=str(vault / "note.md"))

    handler.on_modified(event)
    handler.on_modified(event)

    assert len(timers) == 1
    assert timers[0].delay == 1
    timers[-1].callback()
    handler._queue.join()
    handler.stop()
    assert imports == [((vault / "note.md",), {})]


def test_empty_file_event_does_not_produce_hard_failure(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Vault" / "empty.md"
    path.parent.mkdir()
    path.touch()
    monkeypatch.setattr(watcher.time, "sleep", lambda _: None)
    handler = _handler(tmp_path)
    handler._import(path)
    assert "Obsidian document is empty" not in caplog.text


def test_empty_file_populated_shortly_after_is_imported(tmp_path, monkeypatch):
    path = tmp_path / "Vault" / "note.md"
    path.parent.mkdir()
    path.touch()
    imported = []

    def sleep(_delay):
        path.write_text("# Note\nContent", encoding="utf-8")

    monkeypatch.setattr(watcher.time, "sleep", sleep)
    monkeypatch.setattr(watcher, "run_pipeline", lambda value: imported.append(value))
    handler = _handler(tmp_path)
    handler._import(path)
    assert imported == [path]


def test_permanently_empty_file_is_skipped(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Vault" / "empty.md"
    path.parent.mkdir()
    path.touch()
    calls = []
    monkeypatch.setattr(watcher.time, "sleep", lambda _: None)
    monkeypatch.setattr(watcher, "run_pipeline", lambda value: calls.append(value))
    handler = _handler(tmp_path)
    handler._import(path)
    assert calls == []
    assert "Skipping empty Markdown file" in caplog.text


def test_deleted_before_processing_file_is_skipped(tmp_path, monkeypatch, caplog):
    path = tmp_path / "Vault" / "deleted.md"
    path.parent.mkdir()
    caplog.set_level("DEBUG")
    monkeypatch.setattr(watcher.time, "sleep", lambda _: None)
    handler = _handler(tmp_path)
    handler._import(path)
    assert "disappeared" in caplog.text


def test_valid_markdown_imports_and_requests_git_sync(tmp_path, monkeypatch):
    path = tmp_path / "Vault" / "note.md"
    path.parent.mkdir()
    path.write_text("# Note\nContent", encoding="utf-8")
    imported = []
    syncs = []
    monkeypatch.setattr(watcher, "run_pipeline", lambda value: imported.append(value))
    handler = _handler(tmp_path)
    monkeypatch.setattr(handler._git_sync, "request", lambda: syncs.append(True))
    handler._import(path)
    assert imported == [path]
    assert syncs == [True]
