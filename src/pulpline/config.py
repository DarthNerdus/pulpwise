"""Configuration helpers. Phase 1: just the default output directory.

Full TOML config + auto-creation lands in Phase 2 alongside `state.py`.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_output_dir() -> Path:
    """Where pulpline writes EPUBs by default.

    Honors `PULPLINE_OUTPUT_DIR` for testing / CI. Otherwise: ~/Sync/Pulpline.
    """
    override = os.environ.get("PULPLINE_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Sync" / "Pulpline"
