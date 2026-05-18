"""
Link understanding — detect URLs in user messages, fetch their content,
and inject page context into the prompt for Claude.

Ported from openclaw's src/link-understanding module.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

MAX_LINKS = int(os.getenv("LINK_MAX_LINKS", "3"))
FETCH_TIMEOUT = int(os.getenv("LINK_FETCH_TIMEOUT", "10"))
MAX_CONTENT_LENGTH = int(os.getenv("LINK_MAX_CONTENT_KB", "50")) * 1024
ENABLED = os.getenv("LINK_UNDERSTANDING_ENABLED", "true").lower() in ("1", "true", "yes")

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*]\((https?://\S+?)\)", re.IGNORECASE)
_BARE_LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_BLOCKED_HOSTS = {"localhost", "0.0.0.0"}


@dataclass
class LinkResult:
    url: str
    content: str
    final_url: str
    error: str | None = None


def _is_blocked_host(hostname: str) -> bool:
    if hostname in _BLOCKED_HOSTS:
        return True
    try:
        addr = ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except ValueError:
        return False


def extract_links(message: str, max_links: int = MAX_LINKS) -> list[str]:
    if not message or not message.strip():
        return []
    sanitized = _MARKDOWN_LINK_RE.sub(" ", message)
    seen: set[str] = set()
    results: list[str] = []
    for match in _BARE_LINK_RE.finditer(sanitized):
        raw = match.group(0).rstrip(".,;:!?)")
        try:
            parsed = urlparse(raw)
            if parsed.scheme not in ("http", "https"):
                continue
            if _is_blocked_host(parsed.hostname or ""):
                continue
        except Exception:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        results.append(raw)
        if len(results) >= max_links:
            break
    return results


async def fetch_link_content(url: str, timeout: int = FETCH_TIMEOUT) -> LinkResult:
    headers = {
        "User-Agent": "TeleChatBot/1.0 (link-understanding)",
        "Accept": "text/*,application/json,application/xhtml+xml,application/xml;q=0.9",
    }
    try:
        session = _get_session()
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True, max_redirects=5,
        ) as resp:
                if resp.status != 200:
                    return LinkResult(url=url, content="", final_url=str(resp.url),
                                     error=f"HTTP {resp.status}")
                content_type = resp.content_type or ""
                if not any(t in content_type for t in ("text/", "json", "xml")):
                    return LinkResult(url=url, content="", final_url=str(resp.url),
                                     error=f"Non-text content: {content_type}")
                body = await resp.content.read(MAX_CONTENT_LENGTH)
                text = body.decode("utf-8", errors="replace").strip()
                if not text:
                    return LinkResult(url=url, content="", final_url=str(resp.url),
                                     error="Empty response")
                return LinkResult(url=url, content=text, final_url=str(resp.url))
    except asyncio.TimeoutError:
        return LinkResult(url=url, content="", final_url=url, error="Timeout")
    except Exception as e:
        return LinkResult(url=url, content="", final_url=url, error=str(e)[:200])


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def understand_links(message: str) -> str | None:
    if not ENABLED:
        return None
    urls = extract_links(message)
    if not urls:
        return None

    tasks = [fetch_link_content(u) for u in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    context_parts: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        if r.error or not r.content:
            continue
        content = r.content
        if "<html" in content.lower() or "<body" in content.lower():
            content = _strip_html(content)
        if len(content) > 4000:
            content = content[:4000] + "…"
        context_parts.append(f"[Content from {r.url}]\n{content}")

    if not context_parts:
        return None
    return "\n\n---\n\n".join(context_parts)
