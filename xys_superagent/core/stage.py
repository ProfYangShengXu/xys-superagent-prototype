"""The seven-row stage table, first matching row wins.

This module is a pure function of a ``Snapshot``.  No clock, no filesystem, no
process state: that is what makes it testable as a table.
"""

from __future__ import annotations

from .types import (
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_READY,
    REPORT_WAITING,
    Snapshot,
    Stage,
)


def stage_of(snapshot: Snapshot) -> Stage:
    """Return the first stage whose condition holds.

    Row order is the contract (see the architecture document, section 7.2):

    1. undelivered     delivery failed, retrying
    2. waiting_answer  an unanswered question exists
    3. waiting_review  the latest report is ready
    4. blocked         latest report is blocked/failed and nobody spoke since
    5. queued          no assignment, or an assignment is still pending
    6. idle            latest report is waiting, or the assignment ended
    7. working         everything else, including "turn ended, background
                       work still running"

    Row 6 must not fire while background work is running: that is exactly the
    40-minute-compile bug this table exists to prevent.
    """

    if snapshot.undelivered:
        return Stage.UNDELIVERED
    if snapshot.pending_question:
        return Stage.WAITING_ANSWER
    if snapshot.last_report_status == REPORT_READY:
        return Stage.WAITING_REVIEW
    if (
        snapshot.last_report_status in (REPORT_BLOCKED, REPORT_FAILED)
        and not snapshot.answered_since_report
    ):
        return Stage.BLOCKED
    if not snapshot.background_running and (
        not snapshot.has_assignment or snapshot.pending_assignment
    ):
        return Stage.QUEUED
    if not snapshot.background_running and (
        snapshot.last_report_status == REPORT_WAITING or snapshot.assignment_done
    ):
        return Stage.IDLE
    return Stage.WORKING
