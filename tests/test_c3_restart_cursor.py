"""C3: a restart does not replay consumed events."""

from __future__ import annotations

from xys_superagent.app.loop import Loop
from xys_superagent.core.types import Record

from conftest import FakeExecutor, ScriptedSource, make_app, make_event


class CursorDroppingStore:
    """Counter-example wrapper: accepts the pass but forgets the cursor."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def append(self, record: Record):
        payload = dict(record.payload)
        if "cursor" in payload:
            payload["cursor"] = {}
        return self.inner.append(Record(record.key, record.kind, payload))

    def since(self, cursor):
        return self.inner.since(cursor)

    def find(self, key):
        return self.inner.find(key)

    def count_tickets(self) -> int:
        return self.inner.count_tickets()


def test_c3_restart_resumes_from_persisted_cursor(store):
    events = [make_event("e1"), make_event("e2", thread="thread-2")]
    first_source = ScriptedSource(events)
    app = make_app(store, {"s": first_source}, FakeExecutor())
    Loop(app).pass_once()
    assert store.count_tickets() == 2

    second_source = ScriptedSource(events)
    app2 = make_app(store, {"s": second_source}, FakeExecutor())
    stats = Loop(app2).pass_once()

    assert second_source.cursors_seen[0] == 2
    assert stats.tickets_created == 0
    assert store.count_tickets() == 2


def test_c3_negative_dropping_the_cursor_is_detected(store):
    """The counter-example: without the cursor the source is read from zero."""

    events = [make_event("e1"), make_event("e2", thread="thread-2")]
    wrapped = CursorDroppingStore(store)
    first_source = ScriptedSource(events)
    Loop(make_app(wrapped, {"s": first_source}, FakeExecutor())).pass_once()

    second_source = ScriptedSource(events)
    Loop(make_app(wrapped, {"s": second_source}, FakeExecutor())).pass_once()

    # The positive test asserts the first cursor is 2.  With the cursor
    # dropped, the restart starts from None, so that assertion would fail.
    assert second_source.cursors_seen[0] is None
    assert second_source.cursors_seen[0] != 2
