"""Renderer ABC. Single implementation in v0.1 (EPUB); abstraction stays thin
until CBZ / reflowed PDF arrive and force the contract to firm up.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pulpline.models import RawArticle


class Renderer(ABC):
    """Convert a `RawArticle` into bytes of a target e-reader format."""

    extension: ClassVar[str]

    @abstractmethod
    def render(self, article: RawArticle) -> bytes:
        """Return the rendered file's bytes. Filename is the sink's responsibility."""
