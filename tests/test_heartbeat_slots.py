"""Heartbeat-based liveness and workspace slots."""

from __future__ import annotations

import os
import time

import pytest

from xys_superagent.app.loop import Loop
from xys_superagent.core.heartbeat import Heartbeat, HeartbeatPolicy, stalled
from xys_superagent.core.slot import SlotBoard
from xys_superagent.core.stage import stage_of
from xys_superagent.core.ticket import Mandate
from xys_superagent.core.types import REPORT_PROGRESS, Report, Stage

from conftest import FakeExecutor, ScriptedSource, make_app, make_event, ticket_fixture


def test_heartbeat_timeout_controls_stalled():
    now = 10_000.0
    policy = HeartbeatPolicy(stall_after_s=60)
    assert stalled(None, now, policy) is False  # not started is queued, not stalled
    assert stalled(now - 30, now, policy) is False
    assert stalled(now - 61, now, policy) is True


def test_heartbeat_ignores_file_time(tmp_path):
    """Fresh mtime plus an old heartbeat is stalled; the reverse is not."""

    path = tmp_path / "artifact.bin"
    path.write_text("still here", encoding="utf-8")
    now = time.time()
    policy = HeartbeatPolicy(stall_after_s=60)

    old = now - 3600
    os.utime(path, (now, now))  # the file looks freshly written
    assert stalled(old, now, policy) is True

    os.utime(path, (old, old))  # the file looks untouched
    assert stalled(now - 1, now, policy) is False


def test_heartbeat_policy_rejects_nonsense():
    with pytest.raises(ValueError):
        HeartbeatPolicy(stall_after_s=0)
    with pytest.raises(ValueError):
        HeartbeatPolicy(stall_after_s=-5)


def test_heartbeat_board_records_beats():
    board = Heartbeat(HeartbeatPolicy(stall_after_s=60))
    board.beat("t1", 100.0)
    assert board.last("t1") == 100.0
    assert board.stalled("t1", 150.0) is False
    assert board.stalled("t1", 200.0) is True
    board.forget("t1")
    assert board.last("t1") is None


def test_slots_one_worker_per_workspace():
    board = SlotBoard()
    assert board.try_acquire("demo", "t1") is True
    assert board.try_acquire("demo", "t2") is False  # no eviction
    assert board.holder("demo") == "t1"
    board.release("t1")
    assert board.try_acquire("demo", "t2") is True


def test_slots_other_workspaces_run_in_parallel():
    board = SlotBoard()
    assert board.try_acquire("ws-a", "t1") is True
    assert board.try_acquire("ws-b", "t2") is True


def test_slots_are_derived_from_ticket_state():
    working = ticket_fixture(id="w", workspace="demo", stage=Stage.WORKING)
    ready = ticket_fixture(id="r", workspace="demo", stage=Stage.WAITING_REVIEW)
    board = SlotBoard.from_tickets([working, ready])
    assert board.holder("demo") == "w"
    assert board.free("other")


def test_background_work_keeps_the_slot_and_the_stage():
    ticket = ticket_fixture(
        id="bg",
        workspace="demo",
        stage=Stage.WORKING,
        assignment_done=True,
        last_report=Report(status=REPORT_PROGRESS, background_running=True),
    )
    ticket.refresh_stage(stage_of(ticket.snapshot()), time.time())
    assert ticket.stage == Stage.WORKING
    assert SlotBoard.from_tickets([ticket]).holder("demo") == "bg"


def test_two_tickets_one_workspace_are_serialised(store):
    first = make_event("e1", workspace="demo", thread="thread-1")
    second = make_event("e2", workspace="demo", thread="thread-2")
    source = ScriptedSource([first, second])
    executor = FakeExecutor()
    executor.reports["A1"] = [
        Report(status=REPORT_PROGRESS, background_running=True),
        Report(status="waiting", background_running=False),
    ]
    loop = Loop(make_app(store, {"s": source}, executor))

    one = loop.pass_once()
    assert one.dispatched == 1  # second waits: same workspace

    two = loop.pass_once()  # A1 says it still has background work
    assert two.dispatched == 0
    assert [work.workspace for _, work in executor.handed] == ["demo"]

    three = loop.pass_once()  # A1 finished its turn; slot is free
    assert three.dispatched == 1
    assert len(executor.handed) == 2


def test_max_rounds_stops_instead_of_retrying_forever(store):
    event = make_event("e1")
    source = ScriptedSource([event])
    executor = FakeExecutor(reports={"A1": [Report(status="blocked", summary="stuck")]})
    loop = Loop(make_app(store, {"s": source}, executor))
    loop.pass_once()
    ticket = next(iter(loop.tickets.values()))
    assert ticket.stage == Stage.BLOCKED
    loop.pass_once()
    assert len(executor.handed) == 1
