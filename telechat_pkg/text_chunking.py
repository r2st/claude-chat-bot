"""
Smart text chunking — split long responses into platform-sized chunks
without breaking code blocks, sentences, or markdown structure.

"""
from __future__ import annotations

import re
from dataclasses import dataclass

TELEGRAM_LIMIT = 4096
DEFAULT_CHUNK_LIMIT = 4000  # slightly under Telegram's 4096 to leave room for footer


@dataclass
class TextChunk:
    text: str
    index: int
    total: int


def chunk_text(
    text: str,
    limit: int = DEFAULT_CHUNK_LIMIT,
    mode: str = "smart",
) -> list[TextChunk]:
    """Split text into chunks respecting markdown structure.

    Modes:
      - "smart": Break at paragraph boundaries, code fence boundaries,
                 or sentence boundaries. Never break inside code fences.
      - "length": Break purely on length (emergency fallback).
    """
    if len(text) <= limit:
        return [TextChunk(text=text, index=0, total=1)]

    if mode == "length":
        return _chunk_by_length(text, limit)

    return _chunk_smart(text, limit)


def _chunk_by_length(text: str, limit: int) -> list[TextChunk]:
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to break at a newline
        idx = text.rfind("\n", 0, limit)
        if idx == -1 or idx < limit // 2:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    total = len(chunks)
    return [TextChunk(text=c, index=i, total=total) for i, c in enumerate(chunks)]


def _chunk_smart(text: str, limit: int) -> list[TextChunk]:
    """Split respecting code fences and paragraph boundaries."""
    # Find all code fence spans so we don't break inside them
    fence_spans = _find_fence_spans(text)

    chunks: list[str] = []
    pos = 0

    while pos < len(text):
        remaining = text[pos:]
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Find the best break point within limit
        candidate = text[pos:pos + limit]
        break_at = _find_best_break(candidate, pos, fence_spans)

        if break_at <= 0:
            # Emergency: hard break at limit
            break_at = limit

        chunk = text[pos:pos + break_at].rstrip()
        if chunk:
            chunks.append(chunk)
        pos += break_at
        # Skip leading whitespace for next chunk
        while pos < len(text) and text[pos] in ("\n", "\r"):
            pos += 1

    total = len(chunks)
    return [TextChunk(text=c, index=i, total=total) for i, c in enumerate(chunks)]


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)


def _find_fence_spans(text: str) -> list[tuple[int, int]]:
    """Find (start, end) spans of code fence blocks."""
    spans: list[tuple[int, int]] = []
    matches = list(_FENCE_RE.finditer(text))
    i = 0
    while i < len(matches):
        open_match = matches[i]
        fence_char = open_match.group(1)[0]
        fence_len = len(open_match.group(1))
        # Find closing fence
        found_close = False
        for j in range(i + 1, len(matches)):
            close_match = matches[j]
            if (close_match.group(1)[0] == fence_char and
                    len(close_match.group(1)) >= fence_len):
                spans.append((open_match.start(), close_match.end()))
                i = j + 1
                found_close = True
                break
        if not found_close:
            # Unclosed fence — extends to end
            spans.append((open_match.start(), len(text)))
            break
    return spans


def _is_inside_fence(pos: int, absolute_pos: int, fence_spans: list[tuple[int, int]]) -> bool:
    """Check if an absolute position is inside a code fence."""
    abs_pos = absolute_pos + pos
    for start, end in fence_spans:
        if start <= abs_pos < end:
            return True
    return False


def _find_best_break(text: str, absolute_offset: int, fence_spans: list[tuple[int, int]]) -> int:
    """Find the best break point in text, avoiding code fences."""
    # Priority 1: Break at a blank line (paragraph boundary)
    for match in re.finditer(r"\n\s*\n", text):
        pos = match.end()
        if pos > len(text) * 0.3 and not _is_inside_fence(pos, absolute_offset, fence_spans):
            return pos

    # Priority 2: Break at a code fence boundary
    for match in _FENCE_RE.finditer(text):
        pos = match.start()
        if pos > len(text) * 0.3:
            return pos

    # Priority 3: Break at a newline
    idx = text.rfind("\n", int(len(text) * 0.3))
    if idx > 0 and not _is_inside_fence(idx, absolute_offset, fence_spans):
        return idx + 1

    # Priority 4: Break at sentence boundary
    for match in re.finditer(r"[.!?]\s+", text):
        pos = match.end()
        if pos > len(text) * 0.5:
            return pos

    return 0  # No good break found
