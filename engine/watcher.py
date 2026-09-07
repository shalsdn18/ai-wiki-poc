"""Watch an Obsidian Vault and import changed Markdown documents."""

from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path
from queue import Empty, Queue

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

import engine.config
from engine.pipeline import run_pipeline

LOGGER = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 1.0
IGNORED_PARTS = {"wiki", ".git", ".obsidian"}


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

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run, name="obsidian-import-worker", daemon=True
        )
        self._worker.start()

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
        LOGGER.info("Import started...")
        try:
            stats = run_pipeline(path)
            LOGGER.info(
                "Import finished... read=%s processed=%s failed=%s",
                stats.read_markdown,
                stats.processed,
                stats.failed,
            )
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


def watch_vault(
    vault_path: Path | None = None,
    wiki_root: Path = Path("wiki"),
    state_path: Path = Path(".obsidian_import_state.json"),
) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=None)
    parser.add_argument("--wiki-root", type=Path, default=Path("wiki"))
    parser.add_argument("--state", type=Path, default=Path(".obsidian_import_state.json"))
    args = parser.parse_args()
    watch_vault(args.vault, args.wiki_root, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
