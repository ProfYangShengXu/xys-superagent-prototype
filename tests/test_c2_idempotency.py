"""C2: the same event delivered twice creates one ticket."""

from __future__ import annotations

import uuid

from xys_superagent.app.loop import Loop
from xys_superagent.core.identity import trigger_key

from conftest import AlwaysSource, FakeExecutor, make_app, make_event


def test_c2_same_event_one_ticket(store):
    event = make_event("dup")
    source = AlwaysSource(event)
    app = make_app(store, {"s": source}, FakeExecutor())
    loop = Loop(app)

    first = loop.pass_once()
    second = loop.pass_once()

    assert first.tickets_created == 1
    assert second.tickets_created == 0
    assert second.duplicate_triggers == 1
    assert store.count_tickets() == 1


def test_c2_duplicate_within_one_batch(store):
    event = make_event("dup")
    source = AlwaysSource(event)
    # two identical events arrive in one batch
    source.next_batch = lambda cursor: ([event, event], 1)  # type: ignore[assignment]
    app = make_app(store, {"s": source}, FakeExecutor())
    stats = Loop(app).pass_once()
    assert stats.tickets_created == 1
    assert store.count_tickets() == 1


def test_c2_key_is_deterministic_and_strict():
    assert trigger_key("file", "e1") == trigger_key("file", "e1")
    assert trigger_key("file", "e1") != trigger_key("file", "e2")
    for bad in (("", "e1"), ("file", ""), ("fil:e", "e1"), ("file", "e:1")):
        try:
            trigger_key(*bad)
        except ValueError:
            continue
        raise AssertionError(f"trigger_key accepted an ambiguous key: {bad!r}")


def test_c2_negative_nonce_key_breaks_the_guard(store, monkeypatch):
    """The counter-example: a nonce in the key must be detected.

    The positive test above asserts ``tickets == 1``.  If the key ever became a
    random value, the second delivery would be treated as new work; this test
    proves that the same flow then creates two tickets, so the assertion in the
    positive test is not vacuous.
    """

    real = trigger_key
    monkeypatch.setattr(
        "xys_superagent.core.identity.trigger_key",
        lambda source_id, external_id: real(source_id, external_id) + ":" + uuid.uuid4().hex,
    )
    # No thread: with a thread, a second message would be treated as context
    # for the first ticket, which is a different (and also correct) guard.
    event = make_event("dup", thread="")
    source = AlwaysSource(event)
    app = make_app(store, {"s": source}, FakeExecutor())
    loop = Loop(app)
    loop.pass_once()
    loop.pass_once()
    assert store.count_tickets() == 2  # the C2 invariant is broken, as expected
