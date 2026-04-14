"""
Client-side web search tool powered by DuckDuckGo (no API key required).

Uses the `ddgs` package (successor to `duckduckgo-search`). Falls back from
news search to text search when rate-limited, ensuring the agent always gets
some results even under heavy usage.
"""

import logging
from datetime import datetime, timezone

from ddgs import DDGS

logger = logging.getLogger(__name__)

# Tool definition passed to Claude — matches the standard tool_use schema.
WEB_SEARCH_TOOL: dict = {
    "name": "web_search",
    "description": (
        "Search the web for current financial news, earnings reports, macro data, "
        "and market analysis. Use specific queries including ticker symbols, company "
        "names, or economic events. Prefer recent news (last 24-48 hours)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — be specific. Include ticker symbols and date context.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (1-10). Default: 5.",
                "default": 5,
            },
            "search_type": {
                "type": "string",
                "enum": ["news", "text"],
                "description": "Use 'news' for recent articles (default), 'text' for general web search.",
                "default": "news",
            },
        },
        "required": ["query"],
    },
}


def execute_web_search(
    query: str,
    max_results: int = 5,
    search_type: str = "news",
) -> list[dict]:
    """
    Execute a DuckDuckGo search and return a list of result dicts.

    Falls back from news to text search if news returns an error (rate limit).

    Returns:
        [{"title": ..., "url": ..., "body": ..., "date": ..., "source": ...}, ...]
    """
    max_results = min(max(1, max_results), 10)  # clamp to 1-10

    raw = _search(query, max_results, search_type)

    # Fallback: if news search failed or returned empty, try text search
    if not raw and search_type == "news":
        logger.info("News search empty/failed for %r, falling back to text search", query)
        raw = _search(query, max_results, "text")

    if not raw:
        return [{"error": "No results found", "query": query}]

    results = []
    for item in raw:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", item.get("href", "")),
            "body": item.get("body", item.get("description", "")),
            "date": item.get("date", ""),
            "source": item.get("source", item.get("publisher", "")),
        })

    logger.debug("web_search(%r, %s) -> %d results", query, search_type, len(results))
    return results


def _search(query: str, max_results: int, search_type: str) -> list[dict]:
    """Execute a single DuckDuckGo search. Returns empty list on failure."""
    try:
        ddgs = DDGS()
        if search_type == "news":
            return list(ddgs.news(query, max_results=max_results))
        else:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        logger.warning("web_search(%r, %s) failed: %s", query, search_type, exc)
        return []
