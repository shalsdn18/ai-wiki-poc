from types import SimpleNamespace

from engine import watcher


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
