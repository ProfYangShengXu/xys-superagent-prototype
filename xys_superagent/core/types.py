"""Domain vocabulary shared by every layer.

``core/`` imports the standard library only.  Everything the outside world
needs in order to name a ticket, a stage or a decision lives here, so that
``ports/`` can depend on ``core/`` without depending on a transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

#: Position in a stream or ledger.  Adapters decide what a cursor is made of
#: (a byte offset, a row number, a timestamp); the core only hands it back to
#: the same port unchanged.
Cursor = int | str

#: Opaque handle returned by an executor.  Only the adapter knows whether it
#: is a process id, a remote identifier or a file name.
Assignment = str


class Stage(str, Enum):
    """The seven stages, in the order the table is evaluated."""

    UNDELIVERED = "undelivered"
    WAITING_ANSWER = "waiting_answer"
    WAITING_REVIEW = "waiting_review"
    BLOCKED = "blocked"
    QUEUED = "queued"
    IDLE = "idle"
    WORKING = "working"

    @property
    def waits_for_person(self) -> bool:
        return self in WAITING_STAGES


PERSON = "person"
BOT = "bot"
ACTOR_KINDS = frozenset({PERSON, BOT})

WAITING_STAGES = frozenset(
    {Stage.UNDELIVERED, Stage.WAITING_ANSWER, Stage.WAITING_REVIEW, Stage.BLOCKED}
)

REPORT_READY = "ready"
REPORT_BLOCKED = "blocked"
REPORT_FAILED = "failed"
REPORT_WAITING = "waiting"
REPORT_PROGRESS = "progress"
REPORT_OUT_OF_SCOPE = "out_of_scope"
REPORT_STATUSES = frozenset(
    {
        REPORT_READY,
        REPORT_BLOCKED,
        REPORT_FAILED,
        REPORT_WAITING,
        REPORT_PROGRESS,
        REPORT_OUT_OF_SCOPE,
    }
)

DECISION_ADMIT = "admit"
DECISION_CANDIDATE = "candidate"
DECISION_REJECT = "reject"


@dataclass(frozen=True)
class WorkRef:
    """One unit of work points at: a workspace, an optional branch, a scope.

    This is deliberately not a message.  A message can ask for anything; a
    ``WorkRef`` is the part that is allowed to reach the executor.
    """

    workspace: str
    branch: str = ""
    scope_paths: tuple[str, ...] = ()
    summary: str = ""
    #: Stable identity of one dispatch attempt.  An executor that supports
    #: request deduplication can use it to make a retry safe after a crash
    #: between "hand off" and "record the handle".
    dispatch_key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, str) or not self.workspace.strip():
            raise ValueError("work.workspace must be a non-empty string")


@dataclass(frozen=True)
class Trigger:
    """One immutable event taken from a source."""

    source_id: str
    external_id: str
    actor: str
    actor_kind: str
    text: str
    work: WorkRef
    #: Default is False: a missing field must never count as an explicit
    #: request.  Only an event that says it mentions the agent may start work.
    mentions_agent: bool = False
    thread: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("trigger.source_id must be a non-empty string")
        if not isinstance(self.external_id, str) or not self.external_id.strip():
            raise ValueError("trigger.external_id must be a non-empty string")
        if self.actor_kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor_kind: {self.actor_kind!r}")


@dataclass(frozen=True)
class Policy:
    """Team-specific authorisation.  The only place permission is decided."""

    allowed_people: frozenset[str] = field(default_factory=frozenset)
    allowed_bots: frozenset[str] = field(default_factory=frozenset)
    agent_name: str = ""
    allowed_actions: frozenset[str] = field(
        default_factory=lambda: frozenset({"read", "write", "commit", "test"})
    )
    max_rounds: int = 3
    allowed_scope_roots: tuple[str, ...] = ()
    default_scope_paths: tuple[str, ...] = (".",)

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("policy.max_rounds must be >= 1")


@dataclass(frozen=True)
class Decision:
    """Result of one admission decision.  There is no fourth kind."""

    kind: str
    reason: str = ""
    where: str = ""

    @property
    def allowed(self) -> bool:
        return self.kind == DECISION_ADMIT

    @property
    def rejected(self) -> bool:
        return self.kind == DECISION_REJECT


@dataclass(frozen=True)
class Report:
    """What an executor says about a ticket.  Never carries permission."""

    status: str = REPORT_PROGRESS
    summary: str = ""
    requested_action: str = ""
    requested_path: str = ""
    segments_done: int = 0
    background_running: bool = False
    at: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise ValueError(f"unknown report status: {self.status!r}")


@dataclass(frozen=True)
class Snapshot:
    """The few facts the stage table is allowed to look at."""

    undelivered: bool = False
    pending_question: str | None = None
    last_report_status: str | None = None
    answered_since_report: bool = True
    has_assignment: bool = False
    pending_assignment: bool = False
    assignment_done: bool = False
    background_running: bool = False


@dataclass(frozen=True)
class TicketView:
    """One line of the attention panel, already reduced for a person."""

    ticket_id: str
    short_name: str
    stage: Stage
    waiting_on: str
    command: str
    waited_since: float = 0.0


@dataclass(frozen=True)
class AttentionView:
    """What a person must look at.  Empty means "nothing to see"."""

    items: tuple[TicketView, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.items


@dataclass(frozen=True)
class Message:
    """A thing said to an executor.

    ``intent="probe"`` asks the executor to report the assignment's current
    state without sending new instructions; ``intent="instruction"`` says
    something new.  Reports and deliveries travel through the same port, which
    is why this field exists at all.
    """

    text: str = ""
    intent: str = "instruction"

    def __post_init__(self) -> None:
        if self.intent not in ("instruction", "probe"):
            raise ValueError(f"unknown message intent: {self.intent!r}")


@dataclass(frozen=True)
class Delivery:
    """Result of one round trip with an executor."""

    ok: bool
    note: str = ""
    report: Report | None = None


@dataclass(frozen=True)
class Record:
    """One durable fact.

    The ledger stores records in order.  A record whose kind is ``pass``
    carries the cursor plus the tickets/triggers/decisions produced in that
    same pass, which is how "cursor and ticket land in one transaction" is
    expressed without widening the port.
    """

    key: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
