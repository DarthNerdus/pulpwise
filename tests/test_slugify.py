"""Tests for filename sanitization."""

from __future__ import annotations

import pytest

from pulpline.util.slugify import MAX_FILENAME_LEN, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hello World", "Hello World"),
        ("File / with / slashes", "File with slashes"),
        ('Quotes "and" colons:', "Quotes and colons"),
        ("control\x00chars\x1f", "control chars"),
        ("trailing dots...", "trailing dots"),
        ("trailing space  ", "trailing space"),
        ("multiple   spaces", "multiple spaces"),
        ("", "untitled"),
        ("    ", "untitled"),
        ("///", "untitled"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_length() -> None:
    very_long = "x" * (MAX_FILENAME_LEN + 100)
    out = sanitize_filename(very_long)
    assert len(out) <= MAX_FILENAME_LEN


def test_sanitize_filename_custom_fallback() -> None:
    assert sanitize_filename("", fallback="default") == "default"
