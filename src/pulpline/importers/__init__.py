"""Bulk-import subscriptions from external services.

Importers are a different architectural shape from Sources: they are one-shot,
interactive, and write to `config.toml`. Sources are recurring, programmatic,
and read from the loaded Config. Each importer is self-contained in its own
module - the core layer doesn't know which importers exist.

Shared utilities (range-prompt parsing, etc.) live here.
"""

from __future__ import annotations


def parse_selection(text: str, total: int) -> set[int]:
    """Parse user range input like '1,3,5-7' or 'all' or 'none' into 1-based indices."""
    s = text.strip().lower()
    if not s or s == "none":
        return set()
    if s == "all":
        return set(range(1, total + 1))

    chosen: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ValueError(f"bad range fragment: {part!r}") from exc
            if lo > hi:
                lo, hi = hi, lo
            chosen.update(i for i in range(lo, hi + 1) if 1 <= i <= total)
        else:
            try:
                i = int(part)
            except ValueError as exc:
                raise ValueError(f"bad index: {part!r}") from exc
            if 1 <= i <= total:
                chosen.add(i)
    return chosen
