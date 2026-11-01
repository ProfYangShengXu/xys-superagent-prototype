"""Liveness is a heartbeat, never a file timestamp.

The failure this module exists to prevent: a 40-minute compile is judged
"stalled" because nothing touched a file for 30 minutes.  Only the executor
knows it is still alive, and it says so by beating.  A fresh mtime proves
nothing about a running job, so no filesystem time is consulted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_STALL_AFTER_S = 1800.0


@dataclass(frozen=True)
class HeartbeatPolicy:
    """How long silence is allowed before a ticket counts as stalled."""

    stall_after_s: float = DEFAULT_STALL_AFTER_S

    def __post_init__(self) -> None:
        if not isinstance(self.stall_after_s, (int, float)) or self.stall_after_s <= 0:
            raise ValueError("heartbeat.stall_after_s must be a positive number")


def stalled(last_beat: float | None, now: float, policy: HeartbeatPolicy) -> bool:
    """True only when a beat was expected and none arrived in time.

    ``None`` means "not started yet", which is queued, not stalled.
    """

    if last_beat is None:
        return False
    return (now - last_beat) > policy.stall_after_s


@dataclass
class Heartbeat:
    """Per-ticket last-beat bookkeeping."""

    policy: HeartbeatPolicy = field(default_factory=HeartbeatPolicy)
    beats: dict[str, float] = field(default_factory=dict)

    def beat(self, ticket_id: str, now: float) -> None:
        self.beats[ticket_id] = now

    def last(self, ticket_id: str) -> float | None:
        return self.beats.get(ticket_id)

    def stalled(self, ticket_id: str, now: float) -> bool:
        return stalled(self.beats.get(ticket_id), now, self.policy)

    def forget(self, ticket_id: str) -> None:
        self.beats.pop(ticket_id, None)
