"""The one surface a person looks at."""

from __future__ import annotations

from typing import Protocol

from ..core.types import AttentionView


class PanelPort(Protocol):
    """Publish the view of what is waiting for a person."""

    def publish(self, view: AttentionView) -> None:
        """Render the view.  An empty view must produce no output."""
