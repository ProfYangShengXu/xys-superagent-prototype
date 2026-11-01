"""Plain text panel: one line per ticket, and nothing else.

An empty view writes zero bytes.  That is the property the panel exists for --
a healthy system must be silent.
"""

from __future__ import annotations

import sys
from typing import TextIO

from ..core.types import AttentionView


class TextPanel:
    """Write the attention view to a stream (standard output by default)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    def render(self, view: AttentionView) -> str:
        if view.empty:
            return ""
        lines = [
            f"{item.short_name} | {item.stage.value} | {item.waiting_on} | {item.command}"
            for item in view.items
        ]
        return "\n".join(lines) + "\n"

    def publish(self, view: AttentionView) -> None:
        text = self.render(view)
        if not text:
            return
        self.stream.write(text)
        self.stream.flush()
