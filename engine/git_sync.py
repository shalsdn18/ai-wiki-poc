"""Safely synchronize generated Wiki changes to the current GitHub main branch."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from queue import Empty, Queue

LOGGER = logging.getLogger(__name__)
COMMIT_MESSAGE = "chore(wiki): sync knowledge updates"
DEFAULT_DEBOUNCE_SECONDS = 30.0


class GitSyncError(RuntimeError):
    """Raised when a sync cannot safely proceed."""


def _enabled(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _seconds(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid Git sync debounce seconds: {value}") from exc
    if result < 0:
        raise ValueError("Git sync debounce seconds must be non-negative")
    return result


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitSyncError(f"git {' '.join(args)} failed: {detail}")
    return result


def _wiki_status(repo_root: Path) -> str:
    return _git("status", "--porcelain", "--", "wiki", cwd=repo_root).stdout


def _ensure_main_is_current(repo_root: Path) -> None:
    _git("fetch", "origin", "main", cwd=repo_root)
    branch = _git("branch", "--show-current", cwd=repo_root).stdout.strip()
    if branch != "main":
        raise GitSyncError(f"Refusing to push from branch {branch!r}; expected 'main'")
    counts = _git(
        "rev-list", "--left-right", "--count", "HEAD...origin/main", cwd=repo_root
    ).stdout.split()
    if len(counts) != 2:
        raise GitSyncError("Could not determine whether origin/main is ahead")
    ahead, behind = (int(value) for value in counts)
    if behind or (ahead and behind):
        raise GitSyncError(
            f"origin/main is not an ancestor of local main (ahead={ahead}, behind={behind}); sync aborted"
        )


def sync_wiki(repo_root: Path = Path(".")) -> bool:
    """Commit and push only Wiki changes. Return whether a commit was created."""
    repo_root = repo_root.resolve()
    if not _wiki_status(repo_root).strip():
        LOGGER.info("No Wiki changes to sync")
        return False

    _ensure_main_is_current(repo_root)
    _git("add", "-A", "--", "wiki", cwd=repo_root)
    staged = _git("diff", "--cached", "--name-only", cwd=repo_root).stdout.splitlines()
    unsafe = [path for path in staged if not (Path(path).parts and Path(path).parts[0] == "wiki")]
    if unsafe:
        raise GitSyncError(f"Refusing to commit non-Wiki paths: {unsafe}")
    if not staged:
        LOGGER.info("No staged Wiki changes to sync")
        return False
    _git("commit", "-m", COMMIT_MESSAGE, cwd=repo_root)
    _git("push", "origin", "main", cwd=repo_root)
    LOGGER.info("Wiki sync pushed %s file(s)", len(staged))
    return True


class GitSyncQueue:
    """Collapse sync requests and process them with one debounced worker."""

    def __init__(
        self,
        repo_root: Path = Path("."),
        debounce_seconds: float | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.debounce_seconds = _seconds(
            (
                os.getenv("GIT_SYNC_DEBOUNCE_SECONDS")
                if debounce_seconds is None
                else str(debounce_seconds)
            ),
            DEFAULT_DEBOUNCE_SECONDS,
        )
        self._queue: Queue[None] = Queue()
        self._pending = False
        self._timer: threading.Timer | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return _enabled(os.getenv("GIT_SYNC_ENABLED"))

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="git-sync-worker", daemon=True)
        self._worker.start()

    def request(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._pending = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._enqueue)
            self._timer.daemon = True
            self._timer.start()

    def _enqueue(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._pending = False
            self._timer = None
        self._queue.put(None)

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                sync_wiki(self.repo_root)
            except Exception:
                LOGGER.exception("Git Wiki sync failed; local changes and commits were preserved")
            finally:
                self._queue.task_done()

    def stop(self, flush: bool = True) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            pending = self._pending
            self._pending = False
        if flush and pending:
            self._queue.put(None)
        self._stop.set()
        if self._worker is not None:
            self._worker.join()
