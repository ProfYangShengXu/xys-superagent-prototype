"""C5: a healthy ticket takes zero bytes on the panel."""

from __future__ import annotations

from io import StringIO

from xys_superagent.adapters.panel_text import TextPanel
from xys_superagent.core.panel import attention_view
from xys_superagent.core.types import (
    REPORT_BLOCKED,
    REPORT_READY,
    REPORT_WAITING,
    AttentionView,
    Stage,
    TicketView,
)


def _view(ticket_id: str, stage: Stage, waited: float = 1.0) -> TicketView:
    return TicketView(
        ticket_id=ticket_id,
        short_name=ticket_id,
        stage=stage,
        waiting_on="一个人",
        command=f"xys_superagent ticket {ticket_id}",
        waited_since=waited,
    )


def test_c5_only_the_four_waiting_stages_survive():
    items = [_view("u", Stage.UNDELIVERED), _view("a", Stage.WAITING_ANSWER)]
    items += [_view("r", Stage.WAITING_REVIEW), _view("b", Stage.BLOCKED)]
    items += [_view("q", Stage.QUEUED), _view("i", Stage.IDLE), _view("w", Stage.WORKING)]

    view = attention_view(items)
    assert sorted(item.ticket_id for item in view.items) == ["a", "b", "r", "u"]


def test_c5_oldest_wait_first():
    view = attention_view(
        [_view("new", Stage.WAITING_REVIEW, waited=50.0), _view("old", Stage.WAITING_REVIEW, waited=5.0)]
    )
    assert [item.ticket_id for item in view.items] == ["old", "new"]


def test_c5_working_ticket_adds_no_bytes():
    stream = StringIO()
    panel = TextPanel(stream)
    waiting = attention_view([_view("r", Stage.WAITING_REVIEW)])

    panel.publish(attention_view([]))
    empty_bytes = len(stream.getvalue())
    assert empty_bytes == 0
    panel.publish(waiting)
    one_line = stream.getvalue()
    assert one_line

    stream2 = StringIO()
    panel2 = TextPanel(stream2)
    panel2.publish(attention_view([_view("r", Stage.WAITING_REVIEW), _view("w", Stage.WORKING)]))
    assert stream2.getvalue() == one_line


def test_c5_publish_called_with_working_only_writes_nothing():
    stream = StringIO()
    TextPanel(stream).publish(attention_view([_view("w", Stage.WORKING)]))
    assert stream.getvalue() == ""


def test_c5_negative_rendering_working_would_add_bytes():
    """The counter-example: the naive panel that renders everything."""

    waiting = [_view("r", Stage.WAITING_REVIEW)]
    with_working = waiting + [_view("w", Stage.WORKING)]
    good = TextPanel(StringIO()).render(attention_view(waiting))
    with_work_good = TextPanel(StringIO()).render(attention_view(with_working))
    assert good == with_work_good

    bad = TextPanel(StringIO()).render(AttentionView(items=tuple(waiting)))
    bad_with_work = TextPanel(StringIO()).render(AttentionView(items=tuple(with_working)))
    assert bad == good
    assert bad_with_work != bad  # the C5 assertion `good == with_work_good` has teeth
