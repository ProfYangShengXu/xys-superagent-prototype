"""The pass loop: one pass, then the next, never two at once.

Ordering is chosen for crash safety, not for the numbers in the design
document.  The ledger commit (cursor + triggers + tickets + decisions) happens
*before* anything is handed to an executor, so a crash can at worst replay an
event; it can never dispatch work the ledger does not know about.  Dispatching
again after such a crash is safe because the executor receives a stable
``dispatch_key`` (the bridge deduplicates on it).

The eight steps below mirror section 7.1 of the architecture document, with
steps 5 and 8 collapsed into "commit before dispatch" + "commit then publish".
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ..core import identity
from ..core.admit import admit
from ..core.heartbeat import Heartbeat
from ..core.panel import attention_view
from ..core.types import AttentionView
from ..core.slot import SlotBoard
from ..core.stage import stage_of
from ..core.ticket import Mandate, Ticket
from ..core.types import (
    DECISION_CANDIDATE,
    DECISION_REJECT,
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_OUT_OF_SCOPE,
    REPORT_READY,
    Cursor,
    Decision,
    Message,
    Record,
    Report,
    Stage,
    Trigger,
    WorkRef,
)


@dataclass
class PassStats:
    fetched: int = 0
    duplicate_triggers: int = 0
    rejected: int = 0
    candidates: int = 0
    admitted: int = 0
    tickets_created: int = 0
    dispatched: int = 0
    reports: int = 0
    handbacks: int = 0
    waited: int = 0
    errors: int = 0

    @property
    def quiet(self) -> bool:
        return not (
            self.tickets_created
            or self.dispatched
            or self.reports
            or self.rejected
            or self.handbacks
            or self.errors
        )


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _canon(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def replay_tickets(store: Any) -> dict[str, Ticket]:
    """Rebuild current ticket state from the ledger alone."""

    tickets: dict[str, Ticket] = {}
    for record in store.since(None):
        payload = record.payload
        if record.kind == "ticket" and "id" in payload:
            ticket = Ticket.from_dict(payload)
            tickets[ticket.id] = ticket
        for item in payload.get("tickets", ()) or ():
            if isinstance(item, dict) and "id" in item:
                ticket = Ticket.from_dict(item)
                tickets[ticket.id] = ticket
    return tickets


class Loop:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.store = app.store
        self.tickets: dict[str, Ticket] = {}
        self.cursors: dict[str, Cursor | None] = {}
        self.heartbeat = Heartbeat(app.heartbeat_policy)
        self._saved: dict[str, str] = {}
        self.recover()

    # -- recovery ----------------------------------------------------------

    def recover(self) -> None:
        self.tickets = replay_tickets(self.store)
        now = self.app.clock()
        for name, source in self.app.sources.items():
            source_id = self.app.source_ids.get(name, getattr(source, "source_id", name))
            found = self.store.find(f"cursor:{source_id}")
            if found is not None:
                self.cursors[source_id] = found.payload.get("value")
        for ticket in self.tickets.values():
            if ticket.heartbeat_at:
                self.heartbeat.beat(ticket.id, ticket.heartbeat_at)
            elif ticket.created_at:
                self.heartbeat.beat(ticket.id, min(ticket.created_at, now))
            self._saved[ticket.id] = _canon(ticket.to_dict())

    # -- one pass ------------------------------------------------------------

    def pass_once(self) -> PassStats:
        now = self.app.clock()
        stats = PassStats()

        # 1. take one batch from every source, keeping each cursor
        incoming: list[tuple[str, list[Trigger], Cursor]] = []
        for name in sorted(self.app.sources):
            source = self.app.sources[name]
            source_id = self.app.source_ids.get(name, name)
            try:
                triggers, new_cursor = source.next_batch(self.cursors.get(source_id))
            except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
                self._record(
                    "source_error",
                    f"source_error:{source_id}:{now}",
                    {"source_id": source_id, "error": _describe(exc), "at": now},
                )
                stats.errors += 1
                continue
            for message in getattr(source, "errors", ()) or ():
                self._record(
                    "source_event_error",
                    f"source_event_error:{source_id}:{now}",
                    {"source_id": source_id, "error": str(message), "at": now},
                )
                stats.errors += 1
            stats.fetched += len(triggers)
            incoming.append((source_id, triggers, new_cursor))

        # 2. identity and deduplication, 3. admission, 4. tickets
        seen: set[str] = set()
        trigger_rows: list[dict[str, Any]] = []
        ticket_rows: list[dict[str, Any]] = []
        rejection_rows: list[dict[str, Any]] = []
        decision_rows: list[dict[str, Any]] = []
        rejected: list[tuple[Trigger, Decision]] = []

        for source_id, triggers, _ in incoming:
            for trigger in triggers:
                try:
                    key = identity.trigger_key(trigger.source_id, trigger.external_id)
                except (TypeError, ValueError) as exc:
                    self._record(
                        "trigger_error",
                        f"trigger_error:{source_id}:{trigger.external_id}:{now}",
                        {"error": _describe(exc), "at": now},
                    )
                    stats.errors += 1
                    continue

                if key in seen:
                    stats.duplicate_triggers += 1
                    continue
                seen.add(key)

                # Idempotency comes first: a redelivery of an event that
                # already produced a ticket must not be mistaken for a new
                # message in the thread.
                existing = self.store.find(key)
                if existing is not None:
                    stats.duplicate_triggers += 1
                    if existing.kind == "trigger" and not self._same_trigger(
                        existing.payload, trigger
                    ):
                        rejection_rows.append(
                            self._rejection_row(
                                trigger,
                                "duplicate_key_different_payload",
                                now,
                                where=(
                                    f"source={trigger.source_id} "
                                    f"thread={trigger.thread or '-'} "
                                    f"actor={trigger.actor}"
                                ),
                            )
                        )
                        stats.rejected += 1
                    continue

                # A message in a ticket's thread is context, not a new request:
                # it may answer a question, it may never widen a mandate.
                context_ticket = self._find_by_thread(trigger.thread)
                if context_ticket is not None:
                    context_ticket.answered_since_report = True
                    context_ticket.pending_question = None
                    context_ticket.assignment_done = True
                    self._record(
                        "context_message",
                        f"context:{context_ticket.id}:{now}",
                        {
                            "ticket_id": context_ticket.id,
                            "actor": trigger.actor,
                            "at": now,
                        },
                    )
                    continue

                decision = admit(trigger, self.app.policy)
                decision_rows.append(
                    {
                        "key": key,
                        "kind": decision.kind,
                        "reason": decision.reason,
                        "where": decision.where,
                    }
                )
                row = {
                    "source_id": trigger.source_id,
                    "external_id": trigger.external_id,
                    "key": key,
                    "ticket_id": None,
                    "actor": trigger.actor,
                    "actor_kind": trigger.actor_kind,
                    "created_at": trigger.created_at,
                }
                if decision.kind == DECISION_REJECT:
                    stats.rejected += 1
                    rejection_rows.append(
                        self._rejection_row(trigger, decision.reason, now, decision.where)
                    )
                    rejected.append((trigger, decision))
                    trigger_rows.append(row)
                elif decision.kind == DECISION_CANDIDATE:
                    stats.candidates += 1
                    trigger_rows.append(row)
                else:
                    stats.admitted += 1
                    ticket = self._make_ticket(key, trigger, now)
                    self.tickets[ticket.id] = ticket
                    ticket_rows.append(ticket.to_dict())
                    stats.tickets_created += 1
                    row["ticket_id"] = ticket.id
                    trigger_rows.append(row)

        # 5. one transaction: new cursor + everything it produced
        cursor_map: dict[str, Any] = {
            source_id: new_cursor for source_id, _, new_cursor in incoming
        }
        if trigger_rows or rejection_rows or cursor_map:
            self.store.append(
                Record(
                    key=f"pass:{int(now * 1000)}",
                    kind="pass",
                    payload={
                        "cursor": cursor_map,
                        "triggers": trigger_rows,
                        "tickets": ticket_rows,
                        "rejections": rejection_rows,
                        "decisions": decision_rows,
                        "at": now,
                    },
                )
            )
            self.cursors.update(cursor_map)

        # 6. a refusal always goes back to where the request came from
        for trigger, decision in rejected:
            note = f"xys_superagent refused this request: {decision.reason} ({decision.where})"
            self._reply(trigger, note, now, stats)

        # 7. hand off work that can start, then collect reports
        self._dispatch(now, stats)
        self._collect(now, stats)

        # 8. settle stages, persist one snapshot, publish
        self._settle(now, stats)
        return stats

    # -- steps ---------------------------------------------------------------

    def _dispatch(self, now: float, stats: PassStats) -> None:
        board = SlotBoard.from_tickets(self.tickets.values())
        ordered = sorted(self.tickets.values(), key=lambda t: (t.created_at, t.id))
        for ticket in ordered:
            if ticket.done:
                continue
            retrying = ticket.undelivered and now >= ticket.retry_at
            if ticket.undelivered and not retrying:
                continue
            need_stage = Stage.QUEUED if not ticket.undelivered else Stage.UNDELIVERED
            ticket.refresh_stage(stage_of(ticket.snapshot()), now)
            if ticket.stage != need_stage:
                continue
            if not ticket.may_start_round():
                ticket.blocked_reason = (
                    f"max_rounds {ticket.mandate.max_rounds} reached; "
                    "stopping instead of retrying forever"
                )
                ticket.refresh_stage(Stage.BLOCKED, now)
                self._record(
                    "rounds_exhausted",
                    f"rounds:{ticket.id}:{now}",
                    {"ticket_id": ticket.id, "at": now},
                )
                stats.errors += 1
                continue
            if not board.try_acquire(ticket.workspace, ticket.id):
                continue
            work = WorkRef(
                workspace=ticket.workspace,
                scope_paths=ticket.mandate.scope_paths,
                summary=ticket.work_summary,
                dispatch_key=f"{ticket.id}:r{ticket.rounds + 1}",
            )
            try:
                assignment = self.app.executor.hand_off(ticket.mandate, work)
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                board.release(ticket.id)
                ticket.mark_undelivered(now, self.app.retry_base_s)
                ticket.refresh_stage(Stage.UNDELIVERED, now)
                self._record(
                    "dispatch_error",
                    f"dispatch_error:{ticket.id}:{now}",
                    {
                        "ticket_id": ticket.id,
                        "workspace": ticket.workspace,
                        "error": _describe(exc),
                        "at": now,
                    },
                )
                stats.errors += 1
                continue
            ticket.begin_round(assignment, now)
            ticket.heartbeat_at = now
            self.heartbeat.beat(ticket.id, now)
            ticket.refresh_stage(stage_of(ticket.snapshot()), now)
            stats.dispatched += 1

    def _collect(self, now: float, stats: PassStats) -> None:
        for ticket in list(self.tickets.values()):
            if not ticket.assignment or ticket.done:
                continue
            try:
                delivery = self.app.executor.deliver(
                    ticket.assignment, Message(intent="probe")
                )
            except Exception as exc:  # noqa: BLE001 - recorded; stall check will fire
                self._record(
                    "executor_error",
                    f"executor_error:{ticket.id}:{now}",
                    {
                        "ticket_id": ticket.id,
                        "assignment": ticket.assignment,
                        "error": _describe(exc),
                        "at": now,
                    },
                )
                stats.errors += 1
                continue

            report = delivery.report
            if report is None:
                ticket.heartbeat_at = now
                self.heartbeat.beat(ticket.id, now)
                continue

            stats.reports += 1
            if report.status == REPORT_OUT_OF_SCOPE:
                self._handback(ticket, report, now, stats)
                continue

            ticket.note_report(report, now)
            ticket.heartbeat_at = now
            self.heartbeat.beat(ticket.id, now)
            if report.status == REPORT_READY:
                stats.waited += 1
            elif report.status in (REPORT_BLOCKED, REPORT_FAILED):
                self._record(
                    "blocked_report",
                    f"blocked:{ticket.id}:{now}",
                    {
                        "ticket_id": ticket.id,
                        "status": report.status,
                        "summary": report.summary,
                        "at": now,
                    },
                )

    def _handback(self, ticket: Ticket, report: Report, now: float, stats: PassStats) -> None:
        """A request outside the mandate is returned, never turned into work."""

        previous = ticket.assignment
        if previous:
            try:
                self.app.executor.stop(previous, "out_of_scope")
            except Exception as exc:  # noqa: BLE001 - the handback itself still stands
                self._record(
                    "stop_error",
                    f"stop_error:{ticket.id}:{now}",
                    {"ticket_id": ticket.id, "error": _describe(exc), "at": now},
                )
                stats.errors += 1
        ticket.note_handback(report)
        stats.handbacks += 1
        self._record(
            "handback",
            f"handback:{ticket.id}:{now}",
            {
                "ticket_id": ticket.id,
                "requested_action": report.requested_action,
                "requested_path": report.requested_path,
                "reason": report.summary,
                "at": now,
            },
        )
        synthetic = Trigger(
            source_id=ticket.source_id,
            external_id=ticket.external_id,
            actor="",
            actor_kind="person",
            text=ticket.work_summary,
            work=WorkRef(workspace=ticket.workspace),
            thread=ticket.thread,
        )
        self._reply(
            synthetic,
            f"the executor asked for {report.requested_action or 'an action'} "
            f"at {report.requested_path or 'an out-of-scope path'}; "
            "that is outside this ticket and was returned",
            now,
            stats,
        )

    def _settle(self, now: float, stats: PassStats) -> None:
        changed: list[dict[str, Any]] = []
        for ticket in self.tickets.values():
            if ticket.assignment and not ticket.stalled:
                if self.heartbeat.stalled(ticket.id, now):
                    ticket.stalled = True
                    ticket.note_report(
                        Report(
                            status=REPORT_BLOCKED,
                            summary=(
                                "heartbeat lost: no beat for more than "
                                f"{self.app.heartbeat_policy.stall_after_s:.0f}s "
                                "(heartbeat, not file time)"
                            ),
                            at=now,
                        ),
                        now,
                    )
                    self._record(
                        "stall",
                        f"stall:{ticket.id}:{now}",
                        {"ticket_id": ticket.id, "at": now},
                    )
                    stats.errors += 1
            ticket.refresh_stage(stage_of(ticket.snapshot()), now)
            payload = ticket.to_dict()
            canonical = _canon(payload)
            if self._saved.get(ticket.id) != canonical:
                changed.append(payload)
                self._saved[ticket.id] = canonical

        if changed:
            self.store.append(
                Record(
                    key=f"tickets:{int(now * 1000)}",
                    kind="batch",
                    payload={"tickets": changed, "at": now},
                )
            )
        views = [ticket.view() for ticket in self.tickets.values()]
        view = attention_view(views)
        # Announce each ticket once per (stage, waiting_on).  Re-printing a list
        # the reader has already seen is how a panel trains its reader to ignore
        # it; a ticket that comes back to attention is announced again.
        announced = getattr(self, "_announced", None)
        if announced is None:
            announced = self._announced = {}
        fresh = []
        for item in view.items:
            mark = (item.stage.value, item.waiting_on)
            if announced.get(item.short_name) != mark:
                announced[item.short_name] = mark
                fresh.append(item)
        if fresh:
            self.app.panel.publish(AttentionView(items=tuple(fresh)))
        stats.waited = max(stats.waited, len(view.items))

    # -- helpers -------------------------------------------------------------

    def _make_ticket(self, key: str, trigger: Trigger, now: float) -> Ticket:
        policy = self.app.policy
        scope = trigger.work.scope_paths or policy.default_scope_paths
        ticket_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
        mandate = Mandate(
            scope_paths=tuple(scope),
            allowed_actions=policy.allowed_actions,
            max_rounds=policy.max_rounds,
            stop_conditions=("out_of_scope", "max_rounds", "heartbeat_lost"),
        )
        return Ticket(
            id=ticket_id,
            key=key,
            source_id=trigger.source_id,
            external_id=trigger.external_id,
            workspace=trigger.work.workspace,
            mandate=mandate,
            work_summary=trigger.work.summary,
            created_at=trigger.created_at or now,
            thread=trigger.thread,
        )

    def _find_by_thread(self, thread: str) -> Ticket | None:
        if not thread:
            return None
        for ticket in self.tickets.values():
            if ticket.thread and ticket.thread == thread:
                return ticket
        return None

    @staticmethod
    def _same_trigger(payload: Any, trigger: Trigger) -> bool:
        if not isinstance(payload, dict):
            return False
        return (
            str(payload.get("actor", "")) == trigger.actor
            and str(payload.get("actor_kind", "")) == trigger.actor_kind
        )

    @staticmethod
    def _rejection_row(
        trigger: Trigger, reason: str, now: float, where: str = ""
    ) -> dict[str, Any]:
        return {
            "source_id": trigger.source_id,
            "external_id": trigger.external_id,
            "reason": reason,
            "where": where,
            "at": now,
        }

    def _reply(self, trigger: Trigger, note: str, now: float, stats: PassStats) -> None:
        source = self.app.sources.get(trigger.source_id)
        if source is None:
            # A source may name itself differently from its config key.
            for name, candidate in self.app.sources.items():
                if self.app.source_ids.get(name) == trigger.source_id:
                    source = candidate
                    break
        if source is None:
            self._record(
                "reply_failed",
                f"reply_failed:{trigger.source_id}:{now}",
                {"source_id": trigger.source_id, "reason": "no such source"},
            )
            stats.errors += 1
            return
        try:
            source.reply(trigger, note)
        except Exception as exc:  # noqa: BLE001 - recorded, and counted
            self._record(
                "reply_failed",
                f"reply_failed:{trigger.source_id}:{now}",
                {
                    "source_id": trigger.source_id,
                    "external_id": trigger.external_id,
                    "error": _describe(exc),
                    "note": note,
                    "at": now,
                },
            )
            stats.errors += 1

    def _record(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        self.store.append(Record(key=key, kind=kind, payload=payload))

    # -- running -------------------------------------------------------------

    def run_forever(
        self,
        interval_s: float | None = None,
        max_passes: int | None = None,
        on_pass: Any = None,
    ) -> int:
        interval = self.app.poll_interval_s if interval_s is None else float(interval_s)
        passes = 0
        while True:
            stats = self.pass_once()
            passes += 1
            if on_pass is not None:
                on_pass(stats, passes)
            if max_passes is not None and passes >= max_passes:
                return passes
            time.sleep(interval)


__all__ = ["Loop", "PassStats", "replay_tickets"]
