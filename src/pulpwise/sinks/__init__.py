"""Output sinks for Readwise Reader and Shiori."""

from __future__ import annotations

from pulpwise.sinks.readwise import PushResult, ReadwiseAuthError, ReadwiseSink
from pulpwise.sinks.shiori import ShioriAuthError, ShioriSink

__all__ = [
    "PushResult",
    "ReadwiseAuthError",
    "ReadwiseSink",
    "ShioriAuthError",
    "ShioriSink",
]
