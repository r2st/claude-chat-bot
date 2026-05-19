"""
Polls — create Telegram polls from bot commands or Claude-generated content.

"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_OPTIONS = 10
MIN_OPTIONS = 2


@dataclass
class PollInput:
    question: str
    options: list[str]
    allows_multiple_answers: bool = False
    is_anonymous: bool = True


def parse_poll_command(text: str) -> PollInput | str:
    """Parse /poll command text.

    Formats:
      /poll Question? | Option 1 | Option 2 | Option 3
      /poll Question?
      Option 1
      Option 2
      Option 3

    Returns PollInput on success, error string on failure.
    """
    text = text.strip()
    if not text:
        return "Usage: `/poll Question? | Option A | Option B`\nor:\n`/poll Question?`\n`Option A`\n`Option B`"

    allows_multiple = False
    is_anonymous = True

    if text.startswith("--multi "):
        allows_multiple = True
        text = text[8:].strip()
    if text.startswith("--public "):
        is_anonymous = False
        text = text[9:].strip()

    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        question = parts[0]
        options = [o for o in parts[1:] if o]
    elif "\n" in text:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) < 3:
            return "Need at least a question and 2 options (one per line)."
        question = lines[0]
        options = lines[1:]
    else:
        return "Usage: `/poll Question? | Option A | Option B`\nor:\n`/poll Question?`\n`Option A`\n`Option B`"

    if not question:
        return "Poll question is required."
    if len(options) < MIN_OPTIONS:
        return f"Need at least {MIN_OPTIONS} options."
    if len(options) > MAX_OPTIONS:
        return f"Maximum {MAX_OPTIONS} options allowed."

    options = [o[:100] for o in options]
    question = question[:300]

    return PollInput(
        question=question,
        options=options,
        allows_multiple_answers=allows_multiple,
        is_anonymous=is_anonymous,
    )


_POLL_PATTERN = re.compile(
    r"\[POLL\]\s*\n?"
    r"Q:\s*(.+?)\n"
    r"((?:[-•]\s*.+\n?)+)",
    re.MULTILINE,
)


def extract_poll_from_response(text: str) -> PollInput | None:
    """Try to detect if Claude's response contains a poll suggestion.

    Looks for patterns like:
      [POLL]
      Q: What should we do?
      - Option A
      - Option B
    """
    match = _POLL_PATTERN.search(text)
    if not match:
        return None
    question = match.group(1).strip()
    options_block = match.group(2)
    options = [
        line.lstrip("-•").strip()
        for line in options_block.strip().split("\n")
        if line.strip()
    ]
    if len(options) < MIN_OPTIONS:
        return None
    return PollInput(question=question, options=options[:MAX_OPTIONS])
