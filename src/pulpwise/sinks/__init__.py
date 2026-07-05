"""Output sinks. Pulp Wise ships exactly one: Readwise Reader."""

from __future__ import annotations

from pulpwise.sinks.readwise import PushResult, ReadwiseAuthError, ReadwiseSink

__all__ = ["PushResult", "ReadwiseAuthError", "ReadwiseSink"]
