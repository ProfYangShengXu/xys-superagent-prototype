"""C4: the seven-row stage table is first-match-wins, checked by fixtures."""

from __future__ import annotations

import pytest

from xys_superagent.core.stage import stage_of
from xys_superagent.core.types import (
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_READY,
    REPORT_WAITING,
    Snapshot,
    Stage,
)

#: Twelve snapshots: every row once, plus the precedence traps that a naive
#: if-chain gets wrong.
CASES = [
    (
        "undelivered",
        Snapshot(undelivered=True, has_assignment=True),
        Stage.UNDELIVERED,
    ),
    (
        "waiting_answer",
        Snapshot(pending_question="which branch?", has_assignment=True),
        Stage.WAITING_ANSWER,
    ),
    (
        "waiting_review",
        Snapshot(last_report_status=REPORT_READY, has_assignment=True, assignment_done=True),
        Stage.WAITING_REVIEW,
    ),
    (
        "review_beats_background",
        Snapshot(
            last_report_status=REPORT_READY,
            has_assignment=True,
            assignment_done=True,
            background_running=True,
        ),
        Stage.WAITING_REVIEW,
    ),
    (
        "blocked",
        Snapshot(
            last_report_status=REPORT_BLOCKED,
            answered_since_report=False,
            has_assignment=True,
            assignment_done=True,
        ),
        Stage.BLOCKED,
    ),
    (
        "spoken_after_blocked_falls_through",
        Snapshot(last_report_status=REPORT_FAILED, answered_since_report=True),
        Stage.QUEUED,
    ),
    ("queued_no_assignment", Snapshot(), Stage.QUEUED),
    (
        "queued_pending_assignment",
        Snapshot(has_assignment=True, pending_assignment=True),
        Stage.QUEUED,
    ),
    (
        "idle_assignment_done",
        Snapshot(has_assignment=True, assignment_done=True),
        Stage.IDLE,
    ),
    (
        "idle_report_waiting",
        Snapshot(last_report_status=REPORT_WAITING, has_assignment=True, assignment_done=True),
        Stage.IDLE,
    ),
    ("working_fresh", Snapshot(has_assignment=True), Stage.WORKING),
    (
        "working_background_after_turn",
        Snapshot(has_assignment=True, assignment_done=True, background_running=True),
        Stage.WORKING,
    ),
]


@pytest.mark.parametrize("name,snapshot,expected", CASES, ids=[case[0] for case in CASES])
def test_c4_stage_table(name, snapshot, expected):
    assert stage_of(snapshot) == expected


def test_c4_table_is_pure():
    snapshot = Snapshot(has_assignment=True)
    assert stage_of(snapshot) == stage_of(snapshot)
    assert stage_of(Snapshot()) == Stage.QUEUED


def test_c4_row_order_matters():
    # If "background running" were checked before "waiting review", this would
    # be WORKING; the table says the review row wins.
    snapshot = Snapshot(
        last_report_status=REPORT_READY,
        has_assignment=True,
        assignment_done=True,
        background_running=True,
    )
    assert stage_of(snapshot) == Stage.WAITING_REVIEW


def _mutated_stage_of(snapshot: Snapshot) -> Stage:
    """The counter-example: treat "background still running" as idle.

    This is the exact bug that once killed a 40-minute compile.  The fixture
    test above must catch it; if the two functions agree on the background
    fixture, the table would be green while production is wrong.
    """

    if (
        snapshot.background_running
        and snapshot.has_assignment
        and not snapshot.pending_question
        and snapshot.last_report_status not in (REPORT_READY, REPORT_BLOCKED, REPORT_FAILED)
    ):
        return Stage.IDLE
    return stage_of(snapshot)


@pytest.mark.parametrize("name,snapshot,expected", CASES, ids=[case[0] for case in CASES])
def test_c4_positive_table_would_catch_background_as_idle(name, snapshot, expected):
    if name == "working_background_after_turn":
        assert _mutated_stage_of(snapshot) != expected
        assert _mutated_stage_of(snapshot) == Stage.IDLE
    else:
        assert stage_of(snapshot) == expected
