"""Domain core: tickets, stages, admission, identity, slots, heartbeat.

Standard library only.  Nothing in this package may import ``ports``,
``adapters`` or ``app``.
"""

from .admit import admit
from .heartbeat import DEFAULT_STALL_AFTER_S, Heartbeat, HeartbeatPolicy, stalled
from .identity import trigger_key
from .panel import attention_view
from .slot import SlotBoard
from .stage import stage_of
from .ticket import Mandate, Ticket, covers_path
from .types import (
    ACTOR_KINDS,
    BOT,
    DECISION_ADMIT,
    DECISION_CANDIDATE,
    DECISION_REJECT,
    PERSON,
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_OUT_OF_SCOPE,
    REPORT_PROGRESS,
    REPORT_READY,
    REPORT_WAITING,
    Assignment,
    AttentionView,
    Cursor,
    Decision,
    Delivery,
    Message,
    Policy,
    Record,
    Report,
    Snapshot,
    Stage,
    TicketView,
    Trigger,
    WAITING_STAGES,
    WorkRef,
)

__all__ = [
    "ACTOR_KINDS",
    "Assignment",
    "AttentionView",
    "BOT",
    "Cursor",
    "DECISION_ADMIT",
    "DECISION_CANDIDATE",
    "DECISION_REJECT",
    "DEFAULT_STALL_AFTER_S",
    "Decision",
    "Delivery",
    "Heartbeat",
    "HeartbeatPolicy",
    "Mandate",
    "Message",
    "PERSON",
    "Policy",
    "REPORT_BLOCKED",
    "REPORT_FAILED",
    "REPORT_OUT_OF_SCOPE",
    "REPORT_PROGRESS",
    "REPORT_READY",
    "REPORT_WAITING",
    "Record",
    "Report",
    "SlotBoard",
    "Snapshot",
    "Stage",
    "Ticket",
    "TicketView",
    "Trigger",
    "WAITING_STAGES",
    "WorkRef",
    "admit",
    "attention_view",
    "covers_path",
    "stage_of",
    "stalled",
    "trigger_key",
]
