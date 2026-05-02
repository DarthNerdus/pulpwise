"""Filename sanitization for cross-platform safety."""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RUN = re.compile(r"\s+")
_TRAILING_DOTS = re.compile(r"[.\s]+$")
MAX_FILENAME_LEN = 200


def sanitize_filename(title: str, fallback: str = "untitled") -> str:
    """Convert an article title into a filesystem-safe filename stem.

    Strips control characters and characters forbidden on Windows/macOS/Linux,
    collapses whitespace, trims trailing dots/spaces (Windows hates those),
    and caps length. Returns `fallback` if the result is empty.
    """
    cleaned = _FORBIDDEN.sub(" ", title)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned).strip()
    cleaned = _TRAILING_DOTS.sub("", cleaned)
    if len(cleaned) > MAX_FILENAME_LEN:
        cleaned = cleaned[:MAX_FILENAME_LEN].rstrip()
    return cleaned or fallback
