import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from engine import watcher


def test_single_instance_lock_is_exclusive_between_processes(tmp_path):
    script = """
import sys, time
from engine.watcher import ALREADY_RUNNING, SingleInstanceLock
lock = SingleInstanceLock()
if not lock.acquire():
    print(ALREADY_RUNNING, flush=True)
    sys.exit(0)
print('ACQUIRED', flush=True)
time.sleep(float(sys.argv[1]))
lock.release()
"""
    env = os.environ.copy()
    repo_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(part for part in (repo_root, env.get("PYTHONPATH")) if part)
    cwd_a = tmp_path / "task-cwd"
    cwd_b = tmp_path / "terminal-cwd"
    cwd_a.mkdir()
    cwd_b.mkdir()
    first = subprocess.Popen(
        [sys.executable, "-c", script, "30"],
        cwd=cwd_a,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "ACQUIRED"
        started = time.monotonic()
        second = subprocess.run(
            [sys.executable, "-c", script, "0"],
            cwd=cwd_b,
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        assert time.monotonic() - started < 3
        assert second.returncode == 0
        assert second.stdout.strip() == watcher.ALREADY_RUNNING

        first.terminate()
        first.wait(timeout=5)
        third = subprocess.run(
            [sys.executable, "-c", script, "0"],
            cwd=cwd_b,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert third.returncode == 0, third.stderr
        assert third.stdout.strip() == "ACQUIRED"
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)


def test_windows_mutex_identity_is_repo_based_not_cwd(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(watcher, "REPO_ROOT", repo_root)
    expected = watcher.mutex_name(repo_root)
    assert expected.startswith(r"Local\AIWikiWatcher_")
    assert len(expected.rsplit("_", 1)[1]) == 64
    alternate_cwd = tmp_path / "different-cwd"
    alternate_cwd.mkdir()
    script = "from engine.watcher import REPO_ROOT, mutex_name; print(mutex_name(REPO_ROOT))"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), env.get("PYTHONPATH")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=alternate_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == expected


def test_manual_watcher_cli_without_vault_rejects_existing_instance(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    python = Path(sys.executable)
    vault = tmp_path / "vault"
    vault.mkdir()
    task_cwd = tmp_path / "task-cwd"
    terminal_cwd = tmp_path / "terminal-cwd"
    task_cwd.mkdir()
    terminal_cwd.mkdir()
    env = os.environ.copy()
    env.pop("OBSIDIAN_VAULT_PATH", None)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), env.get("PYTHONPATH")) if part
    )
    first = subprocess.Popen(
        [str(python), "-m", "engine.watcher", "--vault", str(vault)],
        cwd=task_cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1)
        assert first.poll() is None
        started = time.monotonic()
        second = subprocess.run(
            [str(python), "-m", "engine.watcher"],
            cwd=terminal_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        elapsed = time.monotonic() - started
        assert second.returncode == 0
        assert second.stdout.strip() == watcher.ALREADY_RUNNING
        assert second.stderr == ""
        assert elapsed < 2
        assert first.poll() is None

        first.terminate()
        first.wait(timeout=5)
        replacement = subprocess.Popen(
            [str(python), "-m", "engine.watcher", "--vault", str(vault)],
            cwd=terminal_cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1)
            assert replacement.poll() is None
        finally:
            if replacement.poll() is None:
                replacement.terminate()
                replacement.wait(timeout=5)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)


def test_single_instance_lock_rejects_second_process_handle(tmp_path):
    first = watcher.SingleInstanceLock(tmp_path / ".watcher.lock")
    second = watcher.SingleInstanceLock(tmp_path / ".watcher.lock")
    assert first.acquire()
    assert not second.acquire()
    second.release()
    first.release()


def test_single_instance_lock_released_after_normal_shutdown(tmp_path):
    path = tmp_path / ".watcher.lock"
    with watcher.SingleInstanceLock(path):
        assert path.exists() is (os.name != "nt")
        assert not watcher.SingleInstanceLock(path).acquire()
    replacement = watcher.SingleInstanceLock(path)
    assert replacement.acquire()
    replacement.release()


def test_stale_lock_file_is_recoverable(tmp_path):
    path = tmp_path / ".watcher.lock"
    path.write_bytes(b"stale")
    lock = watcher.SingleInstanceLock(path)
    assert lock.acquire()
    lock.release()


def test_unexpected_shutdown_does_not_permanently_block_future_start(tmp_path):
    path = tmp_path / ".watcher.lock"
    first = watcher.SingleInstanceLock(path)
    assert first.acquire()
    first.release()
    replacement = watcher.SingleInstanceLock(path)
    assert replacement.acquire()
    replacement.release()


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


def test_vector_failure_status_does_not_block_git_sync(tmp_path, monkeypatch):
    path = tmp_path / "Vault" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Note\nContent", encoding="utf-8")
    syncs = []
    monkeypatch.setattr(
        watcher,
        "run_pipeline",
        lambda value: SimpleNamespace(
            failed=0, vector_status="unavailable", read_markdown=1, processed=1
        ),
    )
    handler = _handler(tmp_path)
    monkeypatch.setattr(handler._git_sync, "request", lambda: syncs.append(True))
    handler._import(path)
    assert syncs == [True]
