"""Ticket and Mandate: what the work is, and how far it may reach.

The mandate is the structural answer to "it changed one more file than asked".
A request that reaches outside ``scope_paths`` is not refused by a sentence in
a prompt; it is refused because the ticket does not contain it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .types import (
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_OUT_OF_SCOPE,
    REPORT_READY,
    REPORT_WAITING,
    Report,
    Snapshot,
    Stage,
    TicketView,
)


def _normalise_path(path: str) -> tuple[bool, tuple[str, ...]] | None:
    """Lexically normalise a workspace-relative path.

    Returns ``(is_absolute, parts)`` or ``None`` when the path is not a
    well-formed, non-escaping path.  ``..`` that would climb above the root is
    rejected rather than resolved: a mandate must not be talked out of its
    boundary by a path string.
    """

    if not isinstance(path, str) or "\x00" in path:
        return None
    raw = path.replace("\\", "/")
    is_absolute = raw.startswith("/")
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(segment)
    return (is_absolute, tuple(parts))


def covers_path(scope: str, path: str) -> bool:
    """True when ``path`` is ``scope`` or lives underneath it.

    An empty scope (``""`` or ``"."``) covers the whole workspace.  A path
    escape, a different absolute root or a sibling with a shared prefix
    (``src/a`` vs ``src/ab``) does not.
    """

    if not isinstance(scope, str) or scope == "":
        # An empty string is ambiguous; only "." and real paths are scopes.
        return False
    scope_norm = _normalise_path(scope)
    path_norm = _normalise_path(path)
    if scope_norm is None or path_norm is None:
        return False
    scope_abs, scope_parts = scope_norm
    path_abs, path_parts = path_norm
    if not scope_parts:
        return True
    if scope_abs != path_abs:
        return False
    return path_parts[: len(scope_parts)] == scope_parts


@dataclass(frozen=True)
class Mandate:
    """The boundary inside which a ticket may act."""

    scope_paths: tuple[str, ...] = ()
    allowed_actions: frozenset[str] = field(
        default_factory=lambda: frozenset({"read", "write", "commit", "test"})
    )
    max_rounds: int = 3
    stop_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("mandate.max_rounds must be >= 1")
        for scope in self.scope_paths:
            if not isinstance(scope, str) or not scope.strip():
                raise ValueError(
                    "mandate.scope_paths entries must be non-empty strings; "
                    "use '.' for the whole workspace"
                )

    def accepts(self, action: str, path: str) -> bool:
        """May this ticket perform ``action`` on ``path``?"""

        if not isinstance(action, str) or action not in self.allowed_actions:
            return False
        if not isinstance(path, str):
            return False
        if not self.scope_paths:
            return False
        return any(covers_path(scope, path) for scope in self.scope_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_paths": list(self.scope_paths),
            "allowed_actions": sorted(self.allowed_actions),
            "max_rounds": self.max_rounds,
            "stop_conditions": list(self.stop_conditions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Mandate":
        return cls(
            scope_paths=tuple(str(p) for p in data.get("scope_paths", ())),
            allowed_actions=frozenset(str(a) for a in data.get("allowed_actions", ())),
            max_rounds=int(data.get("max_rounds", 3)),
            stop_conditions=tuple(str(s) for s in data.get("stop_conditions", ())),
        )


@dataclass
class Ticket:
    """One unit of work, with its boundary and its current stage."""

    id: str
    key: str
    source_id: str
    external_id: str
    workspace: str
    mandate: Mandate
    work_summary: str = ""
    thread: str = ""
    created_at: float = 0.0
    stage: Stage = Stage.QUEUED
    assignment: str | None = None
    rounds: int = 0
    last_report: Report | None = None
    pending_question: str | None = None
    answered_since_report: bool = True
    assignment_done: bool = False
    background_running: bool = False
    undelivered: bool = False
    retry_at: float = 0.0
    retry_attempts: int = 0
    heartbeat_at: float = 0.0
    handbacks: int = 0
    stalled: bool = False
    done: bool = False
    blocked_reason: str = ""
    waited_since: float = 0.0

    # -- boundary ---------------------------------------------------------

    def accepts(self, action: str, path: str) -> bool:
        return self.mandate.accepts(action, path)

    def may_start_round(self) -> bool:
        return self.rounds < self.mandate.max_rounds

    # -- state transitions -------------------------------------------------

    def begin_round(self, assignment: str, now: float) -> None:
        self.rounds += 1
        self.assignment = assignment
        self.undelivered = False
        self.retry_at = 0.0
        self.retry_attempts = 0
        self.assignment_done = False

    def mark_undelivered(self, now: float, base_delay: float = 2.0) -> None:
        self.undelivered = True
        self.retry_attempts += 1
        delay = min(60.0, base_delay * (2 ** (self.retry_attempts - 1)))
        self.retry_at = now + delay

    def note_report(self, report: Report, now: float) -> None:
        """Fold an executor report into the ticket.

        ``out_of_scope`` never reaches this method: the loop records it as a
        handback instead, because a handback is not a state of the work.
        """

        if report.status == REPORT_OUT_OF_SCOPE:
            raise ValueError("out_of_scope must be handled as a handback")
        self.last_report = report
        self.background_running = report.background_running
        self.answered_since_report = False
        if report.status == REPORT_READY:
            self.assignment_done = True
        elif report.status in (REPORT_BLOCKED, REPORT_FAILED):
            self.assignment_done = True
            self.blocked_reason = report.summary
        elif report.status == REPORT_WAITING:
            self.assignment_done = True
            self.pending_question = None

    def note_handback(self, report: Report) -> None:
        self.handbacks += 1
        self.assignment_done = True
        self.assignment = None
        self.pending_question = None
        self.blocked_reason = report.summary or "out_of_scope"
        # A returned request must stop here: leaving the ticket merely
        # "unassigned" would put it back in the queue and dispatch it again.
        self.last_report = Report(
            status=REPORT_BLOCKED,
            summary=self.blocked_reason,
            requested_action=report.requested_action,
            requested_path=report.requested_path,
            at=report.at,
        )
        self.answered_since_report = False

    def snapshot(self, pending_assignment: bool = False) -> Snapshot:
        # The freshest word on background work is the last report; the flag on
        # the ticket is only a cache of it.  Reading the cache alone is how a
        # 40-minute compile gets reported as "waiting".
        background_running = self.background_running
        if self.last_report is not None:
            background_running = bool(self.last_report.background_running)
        return Snapshot(
            undelivered=self.undelivered,
            pending_question=self.pending_question,
            last_report_status=self.last_report.status if self.last_report else None,
            answered_since_report=self.answered_since_report,
            has_assignment=bool(self.assignment),
            pending_assignment=pending_assignment,
            assignment_done=self.assignment_done,
            background_running=background_running,
        )

    def refresh_stage(self, stage: Stage, now: float) -> None:
        if stage != self.stage:
            self.stage = stage
            if stage in (
                Stage.UNDELIVERED,
                Stage.WAITING_ANSWER,
                Stage.WAITING_REVIEW,
                Stage.BLOCKED,
            ):
                self.waited_since = now

    # -- projection for the panel ---------------------------------------------

    def waiting_on(self) -> str:
        if self.stage == Stage.UNDELIVERED:
            return f"重新投递（第 {self.retry_attempts} 次，{max(0.0, self.retry_at):.0f} 到期）"
        if self.stage == Stage.WAITING_ANSWER:
            return f"回答「{(self.pending_question or '')[:40]}」"
        if self.stage == Stage.WAITING_REVIEW:
            summary = self.last_report.summary if self.last_report else ""
            return f"审阅「{summary[:40]}」"
        if self.stage == Stage.BLOCKED:
            reason = self.blocked_reason or (
                self.last_report.summary if self.last_report else ""
            )
            return f"处理「{reason[:40]}」"
        return ""

    def view(self) -> TicketView:
        return TicketView(
            ticket_id=self.id,
            short_name=self.id,
            stage=self.stage,
            waiting_on=self.waiting_on(),
            command=f"xys_superagent ticket {self.id}",
            waited_since=self.waited_since,
        )

    # -- durability ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "source_id": self.source_id,
            "external_id": self.external_id,
            "workspace": self.workspace,
            "mandate": self.mandate.to_dict(),
            "work_summary": self.work_summary,
            "thread": self.thread,
            "created_at": self.created_at,
            "stage": self.stage.value,
            "assignment": self.assignment,
            "rounds": self.rounds,
            "last_report": (
                {
                    "status": self.last_report.status,
                    "summary": self.last_report.summary,
                    "requested_action": self.last_report.requested_action,
                    "requested_path": self.last_report.requested_path,
                    "segments_done": self.last_report.segments_done,
                    "background_running": self.last_report.background_running,
                    "at": self.last_report.at,
                }
                if self.last_report
                else None
            ),
            "pending_question": self.pending_question,
            "answered_since_report": self.answered_since_report,
            "assignment_done": self.assignment_done,
            "background_running": self.background_running,
            "undelivered": self.undelivered,
            "retry_at": self.retry_at,
            "retry_attempts": self.retry_attempts,
            "heartbeat_at": self.heartbeat_at,
            "handbacks": self.handbacks,
            "stalled": self.stalled,
            "done": self.done,
            "blocked_reason": self.blocked_reason,
            "waited_since": self.waited_since,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Ticket":
        report_data = data.get("last_report")
        report = (
            Report(
                status=str(report_data.get("status", "progress")),
                summary=str(report_data.get("summary", "")),
                requested_action=str(report_data.get("requested_action", "")),
                requested_path=str(report_data.get("requested_path", "")),
                segments_done=int(report_data.get("segments_done", 0)),
                background_running=bool(report_data.get("background_running", False)),
                at=float(report_data.get("at", 0.0)),
            )
            if report_data
            else None
        )
        return cls(
            id=str(data["id"]),
            key=str(data["key"]),
            source_id=str(data["source_id"]),
            external_id=str(data["external_id"]),
            workspace=str(data["workspace"]),
            mandate=Mandate.from_dict(data.get("mandate", {})),
            work_summary=str(data.get("work_summary", "")),
            thread=str(data.get("thread", "")),
            created_at=float(data.get("created_at", 0.0)),
            stage=Stage(str(data.get("stage", Stage.QUEUED.value))),
            assignment=data.get("assignment"),
            rounds=int(data.get("rounds", 0)),
            last_report=report,
            pending_question=data.get("pending_question"),
            answered_since_report=bool(data.get("answered_since_report", True)),
            assignment_done=bool(data.get("assignment_done", False)),
            background_running=bool(data.get("background_running", False)),
            undelivered=bool(data.get("undelivered", False)),
            retry_at=float(data.get("retry_at", 0.0)),
            retry_attempts=int(data.get("retry_attempts", 0)),
            heartbeat_at=float(data.get("heartbeat_at", 0.0)),
            handbacks=int(data.get("handbacks", 0)),
            stalled=bool(data.get("stalled", False)),
            done=bool(data.get("done", False)),
            blocked_reason=str(data.get("blocked_reason", "")),
            waited_since=float(data.get("waited_since", 0.0)),
        )
