"""FastMCP stdio server exposing the existing AI Wiki API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("AI Wiki")


def _api_url() -> str:
    return os.getenv("AI_WIKI_API_URL", "http://localhost:8000").rstrip("/")


def _request(method: str, path: str, **kwargs: Any) -> Any:
    response = httpx.request(method, f"{_api_url()}{path}", timeout=60, **kwargs)
    response.raise_for_status()
    return response.json()


@mcp.tool()
def search_notes(query: str) -> list[dict[str, Any]]:
    """Search notes semantically using the existing FastAPI endpoint."""
    return _request("GET", "/semantic-search", params={"q": query})


@mcp.tool()
def ask_wiki(question: str) -> dict[str, Any]:
    """Ask the Wiki a grounded question and return its answer and sources."""
    return _request("POST", "/chat", json={"question": question})


@mcp.tool()
def recent_notes(limit: int = 10) -> list[dict[str, Any]]:
    """Return recently imported notes."""
    return _request("GET", "/recent", params={"limit": limit})


@mcp.tool()
def stats() -> dict[str, Any]:
    """Return Wiki document and category statistics."""
    return _request("GET", "/stats")


@mcp.tool()
def import_now() -> dict[str, Any]:
    """Run the existing import pipeline immediately."""
    return _request("POST", "/import")


if __name__ == "__main__":
    mcp.run(transport="stdio")
