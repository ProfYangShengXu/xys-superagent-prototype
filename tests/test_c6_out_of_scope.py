"""C6: an out-of-scope request is handed back, never widened."""

from __future__ import annotations

import time

from xys_superagent.app.loop import Loop
from xys_superagent.core.types import REPORT_OUT_OF_SCOPE, Record, Report, Stage

from conftest import FakeExecutor, ScriptedSource, make_app, make_event


def _handback_report() -> Report:
    return Report(
        status=REPORT_OUT_OF_SCOPE,
        summary="needs to touch the identity encoder",
        requested_action="write",
        requested_path="src/identity/codec.py",
        at=1001.0,
    )


def test_c6_out_of_scope_is_handed_back(store):
    event = make_event("e1", scope=("src/parser",))
    source = ScriptedSource([event])
    executor = FakeExecutor(reports={"A1": [_handback_report()]})
    app = make_app(store, {"s": source}, executor)

    loop = Loop(app)
    stats = loop.pass_once()

    assert stats.handbacks == 1
    assert stats.tickets_created == 1
    assert store.count_tickets() == 1, "an out-of-scope request must not create a ticket"

    ticket = next(iter(loop.tickets.values()))
    assert ticket.mandate.scope_paths == ("src/parser",)
    assert ticket.rounds == 1
    assert ticket.handbacks == 1
    assert ticket.stage == Stage.BLOCKED

    handbacks = [r for r in store.since(None) if r.kind == "handback"]
    assert len(handbacks) == 1
    assert handbacks[0].payload["requested_path"] == "src/identity/codec.py"
    assert handbacks[0].payload["reason"]

    assert executor.stopped and executor.stopped[0][1] == "out_of_scope"
    assert len(source.replies) == 1
    assert "outside this ticket" in source.replies[0][1]


def test_c6_handback_does_not_restart_the_work(store):
    event = make_event("e1", scope=("src/parser",))
    source = ScriptedSource([event])
    executor = FakeExecutor(reports={"A1": [_handback_report()]})
    loop = Loop(make_app(store, {"s": source}, executor))
    loop.pass_once()

    loop.pass_once()
    assert len(executor.handed) == 1, "a returned ticket must not be dispatched again"
    assert store.count_tickets() == 1


def test_c6_negative_widening_would_create_a_second_ticket(store):
    """The counter-example: obey the out-of-scope request instead of returning it."""

    event = make_event("e1", scope=("src/parser",))
    source = ScriptedSource([event])
    loop = Loop(make_app(store, {"s": source}, FakeExecutor(reports={"A1": [_handback_report()]})))
    loop.pass_once()
    assert store.count_tickets() == 1

    # This is the forbidden behaviour: treat the request as new work with a
    # widened scope.  It must make the count go up, which is what the positive
    # assertion above rules out.
    widened = make_event("e1-extra", scope=("src/identity",))
    now = time.time()
    ticket = loop._make_ticket("file:e1-extra", widened, now)
    store.append(
        Record(
            key="bad:widened",
            kind="batch",
            payload={"tickets": [ticket.to_dict()]},
        )
    )
    assert store.count_tickets() == 2
    assert ticket.mandate.scope_paths == ("src/identity",)
