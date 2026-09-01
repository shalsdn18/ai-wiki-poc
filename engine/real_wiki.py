"""Run the existing Wiki pipeline independently for each official provider."""

import logging
import time
from collections.abc import Callable
from pathlib import Path

from google.genai.errors import APIError

from collectors.real_collector import collect_all
from engine.wiki_poc import GeminiAnalyzer, load_state, run_pipeline

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3

OUTPUTS = {
    "openai": Path("wiki/openai.md"),
    "gemini": Path("wiki/gemini.md"),
    "claude": Path("wiki/claude.md"),
}


def _run_with_retry(
    provider: str,
    operation: Callable[[], bool],
    sleep: Callable[[float], None],
) -> bool:
    """Retry transient Gemini errors without hiding the final cause."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return operation()
        except APIError as exc:
            if exc.code not in RETRYABLE_STATUS_CODES or attempt == MAX_ATTEMPTS:
                raise
            delay = float(2 ** (attempt - 1))
            LOGGER.warning(
                "%s provider Gemini request failed with HTTP %s; "
                "retrying in %.0f second(s) (attempt %s/%s)",
                provider,
                exc.code,
                delay,
                attempt,
                MAX_ATTEMPTS,
            )
            sleep(delay)
    raise AssertionError("retry loop exited unexpectedly")


def update_all(
    analyzer=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, bool]:
    collected = collect_all()
    results: dict[str, bool] = {}
    shared_analyzer = analyzer
    for provider, path in OUTPUTS.items():
        sources = collected[provider]
        seen = set(load_state(path)["seen_hashes"])
        has_new = any(source.fingerprint not in seen for source in sources)
        if not has_new:
            results[provider] = False
            continue
        try:
            if shared_analyzer is None:
                shared_analyzer = GeminiAnalyzer()
            results[provider] = _run_with_retry(
                provider,
                lambda: run_pipeline(sources, shared_analyzer, path),
                sleep,
            )
        except Exception as exc:
            results[provider] = False
            status = getattr(exc, "code", None)
            reason = f"HTTP {status}: {exc}" if status is not None else str(exc)
            LOGGER.error("%s provider failed; continuing: %s", provider, reason)
    return results


def main() -> int:
    results = update_all()
    for provider, updated in results.items():
        print(f"{provider}: {'updated' if updated else 'unchanged'} ({OUTPUTS[provider]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
