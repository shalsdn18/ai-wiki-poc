from types import SimpleNamespace

import pytest

from engine import git_sync


def _result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_wiki_only_changes_are_staged_and_pushed(monkeypatch, tmp_path):
    commands = []

    def fake_git(*args, cwd):
        commands.append(args)
        if args[:3] == ("status", "--porcelain", "--"):
            return _result(" M wiki/ai/index.md\n")
        if args[:2] == ("branch", "--show-current"):
            return _result("main\n")
        if args[:3] == ("rev-list", "--left-right", "--count"):
            return _result("1 0\n")
        if args[:3] == ("diff", "--cached", "--name-only"):
            return _result("wiki/ai/index.md\n")
        return _result()

    monkeypatch.setattr(git_sync, "_git", fake_git)
    assert git_sync.sync_wiki(tmp_path)
    assert ("add", "-A", "--", "wiki") in commands
    assert ("commit", "-m", git_sync.COMMIT_MESSAGE) in commands
    assert ("push", "origin", "main") in commands


def test_non_wiki_files_are_never_staged(monkeypatch, tmp_path):
    commands = []

    def fake_git(*args, cwd):
        commands.append(args)
        if args[:3] == ("status", "--porcelain", "--"):
            return _result(" M wiki/index.md\n")
        if args[:2] == ("branch", "--show-current"):
            return _result("main\n")
        if args[:3] == ("rev-list", "--left-right", "--count"):
            return _result("1 0\n")
        if args[:3] == ("diff", "--cached", "--name-only"):
            return _result("wiki/index.md\napp.py\n")
        return _result()

    monkeypatch.setattr(git_sync, "_git", fake_git)
    with pytest.raises(git_sync.GitSyncError):
        git_sync.sync_wiki(tmp_path)
    assert ("add", "-A", "--", "wiki") in commands
    assert not any(command[:2] == ("commit", "-m") for command in commands)


def test_no_changes_does_not_commit(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(
        git_sync,
        "_git",
        lambda *args, cwd: (commands.append(args) or _result()),
    )
    assert not git_sync.sync_wiki(tmp_path)
    assert len(commands) == 1


def test_remote_ahead_aborts_before_staging(monkeypatch, tmp_path):
    commands = []

    def fake_git(*args, cwd):
        commands.append(args)
        if args[:3] == ("status", "--porcelain", "--"):
            return _result(" M wiki/index.md\n")
        if args[:2] == ("branch", "--show-current"):
            return _result("main\n")
        if args[:3] == ("rev-list", "--left-right", "--count"):
            return _result("0 1\n")
        return _result()

    monkeypatch.setattr(git_sync, "_git", fake_git)
    with pytest.raises(git_sync.GitSyncError, match="origin/main"):
        git_sync.sync_wiki(tmp_path)
    assert not any(command[:2] == ("add", "-A") for command in commands)


def test_push_failure_preserves_commit_and_files(monkeypatch, tmp_path):
    commands = []

    def fake_git(*args, cwd):
        commands.append(args)
        if args[:3] == ("status", "--porcelain", "--"):
            return _result(" D wiki/removed.md\n")
        if args[:2] == ("branch", "--show-current"):
            return _result("main\n")
        if args[:3] == ("rev-list", "--left-right", "--count"):
            return _result("1 0\n")
        if args[:3] == ("diff", "--cached", "--name-only"):
            return _result("wiki/removed.md\n")
        if args[:1] == ("push",):
            raise git_sync.GitSyncError("push failed")
        return _result()

    monkeypatch.setattr(git_sync, "_git", fake_git)
    with pytest.raises(git_sync.GitSyncError):
        git_sync.sync_wiki(tmp_path)
    assert ("commit", "-m", git_sync.COMMIT_MESSAGE) in commands
    assert not any(command[0] == "reset" for command in commands)


def test_queue_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("GIT_SYNC_ENABLED", raising=False)
    queue = git_sync.GitSyncQueue(tmp_path, debounce_seconds=0)
    queue.start()
    queue.request()
    queue.stop()
    assert queue._worker is None


def test_duplicate_requests_collapse(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_SYNC_ENABLED", "true")
    timers = []

    class FakeTimer:
        def __init__(self, delay, callback):
            self.delay = delay
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(git_sync.threading, "Timer", FakeTimer)
    queue = git_sync.GitSyncQueue(tmp_path, debounce_seconds=30)
    queue.request()
    queue.request()
    assert len(timers) == 2
    assert queue._pending
    queue.stop(flush=False)
