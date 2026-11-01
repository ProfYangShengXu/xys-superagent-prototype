"""Where events come from.

The interface knows that events arrive in batches behind a cursor.  It does
not know which service produced them, and it must not: adding a source means
adding an adapter, not editing this file.
"""

from __future__ import annotations

from typing import Protocol

from ..core.types import Cursor, Trigger


class SourcePort(Protocol):
    """A stream of immutable events with a resumable read position."""

    def next_batch(self, cursor: Cursor | None) -> tuple[list[Trigger], Cursor]:
        """Return events after ``cursor`` and the cursor to resume from."""

    def acknowledge(self, cursor: Cursor) -> None:
        """Mark everything up to ``cursor`` as consumed."""

    def reply(self, trigger: Trigger, note: str) -> None:
        """Say one short thing back to where the event came from.

        Required by the authorship rule: a refused request that is only written
        to the ledger will be repeated.  The person who asked must hear why.
        """
