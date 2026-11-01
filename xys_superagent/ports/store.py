"""The ledger.

Append a fact, read facts after a cursor, look one up by key.  Storage is an
implementation detail: this interface says nothing about files, tables or
formats.
"""

from __future__ import annotations

from typing import Protocol

from ..core.types import Cursor, Record


class StorePort(Protocol):
    """An append-only, resumable record of what happened."""

    def append(self, record: Record) -> Cursor:
        """Persist one record (and everything it carries) atomically."""

    def since(self, cursor: Cursor | None) -> list[Record]:
        """Return records after ``cursor`` in append order."""

    def find(self, key: str) -> Record | None:
        """Return the record for ``key``, or ``None``."""
