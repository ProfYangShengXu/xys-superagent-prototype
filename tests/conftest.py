"""Shared fixtures and doubles for the test suite."""

from __future__ import annotations

import json
import sys
import time
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xys_superagent.adapters.panel_text import TextPanel  # noqa: E402
from xys_superagent.adapters.store_sqlite import SqliteStore  # noqa: E402
from xys_superagent.app.build import App  # noqa: E402
from xys_superagent.core.heartbeat import HeartbeatPolicy  # noqa: E402
from xys_superagent.core.ticket import Mandate, Ticket  # noqa: E402
from xys_superagent.core.types import (  # noqa: E402
    Delivery,
    Policy,
    Report,
    Trigger,
    WorkRef,
)


@pytest.fixture
def store(tmp_path: Path):
    database = SqliteStore(str(tmp_path / "ledger.db"))
    yield database
    database.close()


def make_event(
    external_id: str = "e1",
    *,
    actor: str = "alice",
    actor_kind: str = "person",
    text: str = "please fix the parser",
    scope=("src/parser",),
    workspace: str = "demo",
    thread: str = "thread-1",
    mentions: bool = True,
    created_at: float = 1000.0,
    source_id: str = "file",
) -> Trigger:
    return Trigger(
        source_id=source_id,
        external_id=external_id,
        actor=actor,
        actor_kind=actor_kind,
        text=text,
        work=WorkRef(
            workspace=workspace,
            scope_paths=tuple(scope),
            summary=text[:80],
        ),
        mentions_agent=mentions,
        thread=thread,
        created_at=created_at,
    )


class ScriptedSource:
    """A source that hands out a fixed list, one cursor position at a time."""

    source_id = "scripted"

    def __init__(self, triggers, batch_size: int = 50) -> None:
        self.triggers = list(triggers)
        self.batch_size = batch_size
        # Adopt the id of the events we hand out: a scripted fixture that claims
        # a different id than its own events makes every reply unroutable.
        if self.triggers:
            self.source_id = self.triggers[0].source_id
        self.cursors_seen: list[object] = []
        self.replies: list[tuple[Trigger, str]] = []
        self.errors: list[str] = []
        self.fail_with: Exception | None = None

    def next_batch(self, cursor):
        self.cursors_seen.append(cursor)
        if self.fail_with is not None:
            raise self.fail_with
        start = int(cursor or 0)
        batch = self.triggers[start : start + self.batch_size]
        return batch, start + len(batch)

    def acknowledge(self, cursor) -> None:
        return None

    def reply(self, trigger: Trigger, note: str) -> None:
        self.replies.append((trigger, note))


class AlwaysSource:
    """Returns the same event on every call and never advances the cursor."""

    source_id = "always"

    def __init__(self, trigger: Trigger) -> None:
        self.trigger = trigger
        self.replies: list[tuple[Trigger, str]] = []
        self.errors: list[str] = []

    def next_batch(self, cursor):
        return [self.trigger], 0

    def acknowledge(self, cursor) -> None:
        return None

    def reply(self, trigger: Trigger, note: str) -> None:
        self.replies.append((trigger, note))


class FakeExecutor:
    """Records hand-offs and replays scripted reports on probe."""

    def __init__(self, reports: dict[str, list[Report]] | None = None) -> None:
        self.handed: list[tuple[object, WorkRef]] = []
        self.stopped: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.reports = reports or {}
        self.fail_with: Exception | None = None

    def hand_off(self, mandate, work: WorkRef) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        handle = f"A{len(self.handed) + 1}"
        self.handed.append((mandate, work))
        return handle

    def deliver(self, assignment: str, message) -> Delivery:
        if message.intent == "probe":
            queue = self.reports.get(assignment, [])
            if queue:
                return Delivery(True, "probe", queue.pop(0))
            return Delivery(True, "probe", None)
        self.sent.append((assignment, message.text))
        return Delivery(True, "sent")

    def stop(self, assignment: str, reason: str) -> None:
        self.stopped.append((assignment, reason))


def make_app(
    store,
    sources,
    executor=None,
    *,
    policy: Policy | None = None,
    panel: TextPanel | None = None,
    stall_after_s: float = 1800.0,
    clock=None,
) -> App:
    source_ids = {name: getattr(source, "source_id", name) for name, source in sources.items()}
    return App(
        config={},
        store=store,
        sources=sources,
        source_ids=source_ids,
        executor=executor or FakeExecutor(),
        panel=panel or TextPanel(StringIO()),
        policy=policy
        or Policy(
            allowed_people=frozenset({"alice", "bob"}),
            allowed_bots=frozenset({"ci-bot"}),
            max_rounds=3,
        ),
        heartbeat_policy=HeartbeatPolicy(stall_after_s),
        poll_interval_s=0.01,
        retry_base_s=2.0,
        clock=clock or time.time,
    )


def write_events(path: Path, events) -> Path:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def ticket_fixture(**overrides) -> Ticket:
    base = dict(
        id="t1",
        key="file:e1",
        source_id="file",
        external_id="e1",
        workspace="demo",
        mandate=Mandate(scope_paths=("src/parser",)),
        work_summary="fix parser",
        created_at=1000.0,
    )
    base.update(overrides)
    return Ticket(**base)
