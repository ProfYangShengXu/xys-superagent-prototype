"""Where work is handed to.

One ticket, one assignment, one opaque handle.  The core never interprets the
handle; only the adapter knows what it points at.
"""

from __future__ import annotations

from typing import Protocol

from ..core.ticket import Mandate
from ..core.types import Assignment, Delivery, Message, WorkRef


class ExecutorPort(Protocol):
    """Something that accepts work and reports on it."""

    def hand_off(self, mandate: Mandate, work: WorkRef) -> Assignment:
        """Start the work and return a handle for later messages."""

    def deliver(self, assignment: Assignment, message: Message) -> Delivery:
        """Send a message, or ask for the current state when probing.

        Reports come back through this same call, which is why ``Delivery``
        may carry one.
        """

    def stop(self, assignment: Assignment, reason: str) -> None:
        """End the work, with a reason kept in the ledger."""
