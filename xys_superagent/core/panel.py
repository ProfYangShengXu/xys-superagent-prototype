"""The attention panel: only what is waiting for a person.

A healthy ticket takes no space.  That is a byte-level contract, not a style
preference: adding a ``working`` ticket must not add a single byte to the
panel output.
"""

from __future__ import annotations

from typing import Iterable

from .types import AttentionView, TicketView, WAITING_STAGES


def attention_view(tickets: Iterable[TicketView]) -> AttentionView:
    """Keep only the four waiting stages, oldest wait first.

    Sorting is by how long the ticket has been waiting, not by creation time:
    the person's question is "what has been stuck longest", not "what is new".
    """

    waiting = [item for item in tickets if item.stage in WAITING_STAGES]
    waiting.sort(key=lambda item: (item.waited_since, item.ticket_id))
    # One line per ticket: a ticket can reach this function twice (live state
    # plus the replay of its own ledger record), and a duplicated row costs the
    # reader exactly the attention this panel exists to save.
    seen: set[str] = set()
    unique = []
    for item in waiting:
        if item.ticket_id in seen:
            continue
        seen.add(item.ticket_id)
        unique.append(item)
    return AttentionView(items=tuple(unique))
