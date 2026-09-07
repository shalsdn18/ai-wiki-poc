"""Delta-gated AI Wiki pipeline with deterministic Markdown rendering."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Protocol, Sequence

import engine.config
from engine.schemas import SourceRecord, WikiUpdatePayload

DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_WIKI_PATH = Path("wiki/ai-wiki.md")


class Analyzer(Protocol):
    def analyze(self, sources: Sequence[SourceRecord]) -> WikiUpdatePayload: ...


class GeminiAnalyzer:
    """google-genai adapter isolated from state and rendering logic."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from google import genai

        resolved_key = api_key or os.getenv("GEMINI_API_KEY")
        if not resolved_key:
            raise RuntimeError("GEMINI_API_KEY is required when new sources are found")
        self._client = genai.Client(api_key=resolved_key)
        self._model = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    def analyze(self, sources: Sequence[SourceRecord]) -> WikiUpdatePayload:
        from google.genai import types

        source_json = json.dumps(
            [source.model_dump(mode="json") for source in sources],
            ensure_ascii=False,
            indent=2,
        )
        prompt = (
            "Analyze only the supplied new sources. Return factual, concise fields "
            "matching the response schema. Do not write Markdown. used_source_ids "
            "must contain only source_id values present below.\n\n"
            f"NEW SOURCES:\n{source_json}"
        )
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=WikiUpdatePayload,
                temperature=0,
            ),
        )
        if response.parsed is not None:
            return WikiUpdatePayload.model_validate(response.parsed)
        return WikiUpdatePayload.model_validate_json(response.text)


def _empty_state() -> dict:
    return {"seen_hashes": [], "current_state": "", "key_facts": [], "recent_changes": [], "history_log": []}


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Invalid Wiki frontmatter: {path}")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"Unclosed Wiki frontmatter: {path}")
    state = _empty_state()
    state.update(json.loads(text[4:end]))
    return state


def _render_list(items: Sequence[str], empty_text: str = "_None yet._") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty_text


def render_markdown(state: dict) -> str:
    frontmatter = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    recent = [
        f"{entry['published_at']} — {entry['summary']} (sources: {', '.join(entry['source_ids'])})"
        for entry in state["recent_changes"]
    ]
    history = [
        f"{entry['published_at']} — {entry['summary']} (sources: {', '.join(entry['source_ids'])})"
        for entry in state["history_log"]
    ]
    return (
        f"---\n{frontmatter}\n---\n\n# AI Wiki\n\n## Current State\n\n"
        f"{state['current_state'] or '_No state established yet._'}\n\n"
        f"## Key Facts\n\n{_render_list(state['key_facts'])}\n\n"
        f"## Recent Changes\n\n{_render_list(recent)}\n\n"
        f"## History Log\n\n{_render_list(history)}\n"
    )


def run_pipeline(sources: Sequence[SourceRecord], analyzer: Analyzer, wiki_path: Path = DEFAULT_WIKI_PATH) -> bool:
    """Process new content and return True only when the Wiki was updated."""
    state = load_state(wiki_path)
    seen = set(state["seen_hashes"])
    new_sources: list[SourceRecord] = []
    batch_hashes: set[str] = set()
    for source in sources:
        if source.fingerprint not in seen and source.fingerprint not in batch_hashes:
            new_sources.append(source)
            batch_hashes.add(source.fingerprint)
    if not new_sources:
        return False

    payload = analyzer.analyze(new_sources)
    valid_ids = {source.source_id for source in new_sources}
    used_ids = list(dict.fromkeys(source_id for source_id in payload.used_source_ids if source_id in valid_ids))
    event_time = max(source.published_at for source in new_sources).isoformat()
    new_entries = [
        {"published_at": event_time, "summary": summary, "source_ids": used_ids}
        for summary in payload.new_changes
    ]
    merged_changes = state["recent_changes"] + new_entries
    merged_changes.sort(key=lambda entry: (entry["published_at"], entry["summary"]), reverse=True)

    state.update(
        seen_hashes=sorted(seen | {source.fingerprint for source in new_sources}),
        current_state=payload.current_state,
        key_facts=payload.key_facts,
        recent_changes=merged_changes[:5],
    )
    state["history_log"].append(
        {"published_at": event_time, "summary": payload.history_summary, "source_ids": used_ids}
    )
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.write_text(render_markdown(state), encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_WIKI_PATH)
    args = parser.parse_args()

    from collectors.mock_collector import collect

    sources = collect()
    seen = set(load_state(args.output)["seen_hashes"])
    if all(source.fingerprint in seen for source in sources):
        print("No new sources; Gemini was not called.")
        return 0
    updated = run_pipeline(sources, GeminiAnalyzer(), args.output)
    print(f"Wiki {'updated' if updated else 'unchanged'}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
