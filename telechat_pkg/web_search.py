"""
Web search — search the web and return results for Claude to use as context.

Supports Brave Search API and Tavily API (configurable via env vars).
Ported from openclaw's src/web-search module.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "auto")  # brave | tavily | auto
MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None


def is_available() -> bool:
    if not WEB_SEARCH_ENABLED:
        return False
    provider = _resolve_provider()
    return provider is not None


def _resolve_provider() -> str | None:
    if SEARCH_PROVIDER == "brave" and BRAVE_API_KEY:
        return "brave"
    if SEARCH_PROVIDER == "tavily" and TAVILY_API_KEY:
        return "tavily"
    if SEARCH_PROVIDER == "auto":
        if BRAVE_API_KEY:
            return "brave"
        if TAVILY_API_KEY:
            return "tavily"
    return None


async def search(query: str, max_results: int = MAX_RESULTS) -> SearchResponse:
    provider = _resolve_provider()
    if not provider:
        return SearchResponse(query=query, error="No search API key configured")

    if provider == "brave":
        return await _search_brave(query, max_results)
    elif provider == "tavily":
        return await _search_tavily(query, max_results)
    return SearchResponse(query=query, error=f"Unknown provider: {provider}")


async def _search_brave(query: str, max_results: int) -> SearchResponse:
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    params = {"q": query, "count": str(max_results)}

    try:
        session = _get_session()
        async with session.get(url, headers=headers, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return SearchResponse(query=query,
                                          error=f"Brave API error {resp.status}: {body[:200]}")
                data = await resp.json()
                results = []
                for item in (data.get("web", {}).get("results", []))[:max_results]:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("description", ""),
                    ))
                return SearchResponse(query=query, results=results)
    except asyncio.TimeoutError:
        return SearchResponse(query=query, error="Search timed out")
    except Exception as e:
        return SearchResponse(query=query, error=str(e)[:200])


async def _search_tavily(query: str, max_results: int) -> SearchResponse:
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "include_answer": False,
    }

    try:
        session = _get_session()
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return SearchResponse(query=query,
                                          error=f"Tavily API error {resp.status}: {body[:200]}")
                data = await resp.json()
                results = []
                for item in (data.get("results", []))[:max_results]:
                    results.append(SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", ""),
                    ))
                return SearchResponse(query=query, results=results)
    except asyncio.TimeoutError:
        return SearchResponse(query=query, error="Search timed out")
    except Exception as e:
        return SearchResponse(query=query, error=str(e)[:200])


def format_results(resp: SearchResponse) -> str:
    if resp.error:
        return f"Search error: {resp.error}"
    if not resp.results:
        return f"No results found for: {resp.query}"
    parts = [f"Web search results for: {resp.query}\n"]
    for i, r in enumerate(resp.results, 1):
        parts.append(f"{i}. [{r.title}]({r.url})\n   {r.snippet}")
    return "\n\n".join(parts)
