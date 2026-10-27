"""Watch an Obsidian Vault and import changed Markdown documents."""

from __future__ import annotations

import argparse
import errno
import hashlib
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, Queue

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from engine.git_sync import GitSyncQueue
from engine.pipeline import run_pipeline

LOGGER = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 1.0
EMPTY_FILE_RETRY_DELAYS = (0.5, 1.0, 2.0)
IGNORED_PARTS = {"wiki", ".git", ".obsidian"}
REPO_ROOT = Path(__file__).resolve().parent.parent
ALREADY_RUNNING = "AI Wiki watcher is already running."
WATCHER_LOG_PATH = REPO_ROOT / "logs" / "watcher.log"


def mutex_name(repo_root: Path = REPO_ROOT) -> str:
    """Return the deterministic Windows mutex name for a canonical repo root."""
    canonical = str(repo_root.resolve()).casefold()
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    return rf"Local\AIWikiWatcher_{digest}"


class SingleInstanceLock:
    """Non-blocking process lock backed by a Windows mutex or POSIX flock."""

    def __init__(self, path: Path | None = None, repo_root: Path = REPO_ROOT) -> None:
        self.path = path.resolve() if path is not None else None
        self.repo_root = repo_root.resolve()
        self._handle = None
        self._kernel32 = None

    def acquire(self) -> bool:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.CreateMutexW.argtypes = (
                ctypes.c_void_p,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            )
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.GetLastError.argtypes = ()
            kernel32.GetLastError.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            name = mutex_name(self.repo_root)
            handle = kernel32.CreateMutexW(None, False, name)
            last_error = kernel32.GetLastError()
            if not handle:
                raise ctypes.WinError(last_error)
            self._kernel32 = kernel32
            self._handle = handle
            if last_error == 183:  # ERROR_ALREADY_EXISTS
                LOGGER.info(
                    "Single-instance mutex already exists: identity=%s pid=%s",
                    name,
                    os.getpid(),
                )
                self.release()
                return False
            LOGGER.info(
                "Single-instance mutex acquired: identity=%s pid=%s",
                name,
                os.getpid(),
            )
            return True

        if self.path is None:
            self.path = REPO_ROOT / ".watcher.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if getattr(exc, "errno", None) not in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                raise
            return False
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                self._kernel32.CloseHandle(self._handle)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle = None
            self._kernel32 = None
            if os.name != "nt":
                # POSIX keeps the Python file object as the flock owner.
                self._handle.close()

    def __enter__(self) -> SingleInstanceLock:  # noqa: PYI034
        if not self.acquire():
            raise RuntimeError(ALREADY_RUNNING)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _should_watch(path: Path, vault_path: Path) -> bool:
    """Return whether a path is an importable Markdown path in the vault."""
    try:
        relative = path.resolve().relative_to(vault_path.resolve())
    except ValueError:
        return False
    parts = {part.casefold() for part in relative.parts}
    return (
        path.suffix.casefold() == ".md"
        and ".obsidian_import_state.json" not in parts
        and not parts & IGNORED_PARTS
    )


def _wait_for_processable_file(
    path: Path,
    retry_delays: tuple[float, ...] = EMPTY_FILE_RETRY_DELAYS,
) -> bool:
    """Wait briefly for Obsidian to finish creating a Markdown file."""
    for attempt in range(len(retry_delays) + 1):
        try:
            if path.stat().st_size > 0 and path.read_text(encoding="utf-8-sig").strip():
                return True
        except FileNotFoundError:
            LOGGER.debug("Markdown file disappeared before import: %s", path)
            return False
        if attempt < len(retry_delays):
            time.sleep(retry_delays[attempt])
    LOGGER.warning("Skipping empty Markdown file after retries: %s", path)
    return False


class ObsidianEventHandler(FileSystemEventHandler):
    def __init__(
        self,
        vault_path: Path,
        wiki_root: Path,
        state_path: Path,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self.vault_path = vault_path
        self.wiki_root = wiki_root
        self.state_path = state_path
        self.debounce_seconds = debounce_seconds
        self._timer: threading.Timer | None = None
        self._pending_paths: set[Path] = set()
        self._known_paths: set[Path] = set()
        self._queue: Queue[Path] = Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._git_sync = GitSyncQueue()

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run, name="obsidian-import-worker", daemon=True
        )
        self._worker.start()
        self._git_sync.start()

    def on_created(self, event: FileSystemEvent) -> None:
        self._changed(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._changed(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._changed(event)

    def _changed(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(getattr(event, "dest_path", event.src_path))
        if not _should_watch(path, self.vault_path):
            return
        LOGGER.info("File changed... %s", path)
        with self._lock:
            if path in self._known_paths:
                return
            self._known_paths.add(path)
            self._pending_paths.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._enqueue_pending)
            self._timer.daemon = True
            self._timer.start()

    def _enqueue_pending(self) -> None:
        with self._lock:
            paths = tuple(self._pending_paths)
            self._pending_paths.clear()
            self._timer = None
        for path in paths:
            self._queue.put(path)

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                path = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._import(path)
            finally:
                with self._lock:
                    self._known_paths.discard(path)
                self._queue.task_done()

    def _import(self, path: Path) -> None:
        if not _wait_for_processable_file(path):
            return
        LOGGER.info("Import started...")
        try:
            stats = run_pipeline(path)
            if stats is None or getattr(stats, "failed", 0) == 0:
                self._git_sync.request()
            LOGGER.info(
                "Import finished... read=%s processed=%s failed=%s",
                stats.read_markdown,
                stats.processed,
                stats.failed,
            )
        except FileNotFoundError:
            LOGGER.debug("Markdown file disappeared before pipeline completed: %s", path)
        except ValueError as exc:
            if str(exc) == "Obsidian document is empty":
                LOGGER.warning("Skipping empty Markdown file: %s", path)
                return
            LOGGER.exception("Import finished... failed")
        except Exception:
            LOGGER.exception("Import finished... failed")

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending_paths.clear()
            self._known_paths.clear()
        self._stop_event.set()
        if self._worker is not None:
            self._worker.join()
        self._git_sync.stop(flush=True)


def watch_vault(
    vault_path: Path | None = None,
    wiki_root: Path = Path("wiki"),
    state_path: Path = Path(".obsidian_import_state.json"),
) -> None:
    try:
        with SingleInstanceLock(repo_root=REPO_ROOT):
            configured = vault_path or Path(os.environ.get("OBSIDIAN_VAULT_PATH", ""))
            if not configured.is_dir():
                raise RuntimeError(f"Obsidian Vault directory does not exist: {configured}")
            configured = configured.resolve()
            handler = ObsidianEventHandler(configured, wiki_root, state_path)
            handler.start()
            observer = Observer()
            observer.schedule(handler, str(configured), recursive=True)
            observer.start()
            LOGGER.info("Watching... %s", configured)
            try:
                observer.join()
            except KeyboardInterrupt:
                LOGGER.info("Stopping watcher...")
            finally:
                handler.stop()
                observer.stop()
                observer.join()
    except RuntimeError as exc:
        if str(exc) != ALREADY_RUNNING:
            raise
        print(str(exc), flush=True)


def main() -> int:
    WATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(
                WATCHER_LOG_PATH,
                maxBytes=2 * 1024 * 1024,
                backupCount=4,
                encoding="utf-8",
            )
        ],
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--wiki-root", type=Path, default=Path("wiki"))
    parser.add_argument("--state", type=Path, default=Path(".obsidian_import_state.json"))
    args = parser.parse_args()
    watch_vault(args.vault, args.wiki_root, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
