"""Slots: one workspace, one ticket doing work.

Two agents editing one repository collide on files; two agents on two
repositories do not.  So the unit of mutual exclusion is the workspace, not
the agent.  A ticket whose turn ended but whose background work is still
running keeps its slot -- otherwise the next ticket starts in the same
directory while a build is still writing there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .types import Stage


@dataclass
class SlotBoard:
    """Who currently holds each workspace."""

    holders: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_tickets(cls, tickets: Iterable[object]) -> "SlotBoard":
        """Rebuild the board from ticket state.

        Deriving instead of remembering means a restart cannot leak a slot:
        the ledger is the only truth.
        """

        holders: dict[str, str] = {}
        for ticket in tickets:
            stage = getattr(ticket, "stage", None)
            workspace = getattr(ticket, "workspace", "")
            ticket_id = getattr(ticket, "id", "")
            if stage == Stage.WORKING and workspace and ticket_id:
                holders.setdefault(workspace, ticket_id)
        return cls(holders=holders)

    def holder(self, workspace: str) -> str | None:
        return self.holders.get(workspace)

    def free(self, workspace: str) -> bool:
        return workspace not in self.holders

    def try_acquire(self, workspace: str, ticket_id: str) -> bool:
        """Take the workspace, or return False without evicting anyone."""

        if not workspace or not ticket_id:
            return False
        current = self.holders.get(workspace)
        if current is not None and current != ticket_id:
            return False
        self.holders[workspace] = ticket_id
        return True

    def release(self, ticket_id: str) -> None:
        for workspace, holder in list(self.holders.items()):
            if holder == ticket_id:
                del self.holders[workspace]

    def release_workspace(self, workspace: str) -> None:
        self.holders.pop(workspace, None)
