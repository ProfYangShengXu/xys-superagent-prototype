"""C7: every refusal carries a reason and returns to where it came from."""

from __future__ import annotations

import pytest

from xys_superagent.app.loop import Loop
from xys_superagent.core.admit import admit
from xys_superagent.core.types import (
    BOT,
    DECISION_ADMIT,
    DECISION_CANDIDATE,
    DECISION_REJECT,
    Policy,
)

from conftest import FakeExecutor, ScriptedSource, make_app, make_event


def _policy(**overrides) -> Policy:
    base = dict(
        allowed_people=frozenset({"alice"}),
        allowed_bots=frozenset({"ci-bot"}),
        max_rounds=3,
    )
    base.update(overrides)
    return Policy(**base)


@pytest.mark.parametrize(
    "name,event,policy,expected",
    [
        ("allowed_person", make_event(), _policy(), DECISION_ADMIT),
        ("outsider", make_event(actor="mallory"), _policy(), DECISION_REJECT),
        (
            "no_explicit_mention",
            make_event(mentions=False),
            _policy(),
            DECISION_REJECT,
        ),
        ("empty_text", make_event(text="   "), _policy(), DECISION_REJECT),
        (
            "allowed_bot_is_only_a_candidate",
            make_event(actor="ci-bot", actor_kind=BOT),
            _policy(),
            DECISION_CANDIDATE,
        ),
        (
            "unknown_bot",
            make_event(actor="other-bot", actor_kind=BOT),
            _policy(),
            DECISION_REJECT,
        ),
        (
            "scope_outside_policy",
            make_event(scope=("docs",)),
            _policy(allowed_scope_roots=("src",), default_scope_paths=("src",)),
            DECISION_REJECT,
        ),
        ("empty_scope_entry", make_event(scope=("",)), _policy(), DECISION_REJECT),
    ],
)
def test_c7_admit_matrix(name, event, policy, expected):
    decision = admit(event, policy)
    assert decision.kind == expected
    assert decision.where, "every decision must say where it was made"
    if decision.kind == DECISION_REJECT:
        assert decision.reason, "every refusal must carry a reason"


def test_c7_unknown_actor_kind_is_refused_at_the_boundary():
    with pytest.raises(ValueError):
        make_event(actor_kind="robot")


def test_c7_rejection_is_recorded_and_sent_back(store):
    event = make_event("e1", actor="mallory")
    source = ScriptedSource([event])
    loop = Loop(make_app(store, {"s": source}, FakeExecutor()))

    stats = loop.pass_once()

    assert stats.rejected == 1
    assert store.count_tickets() == 0

    rejections = []
    for record in store.since(None):
        rejections.extend(record.payload.get("rejections", ()) or ())
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "actor_not_allowed"
    assert rejections[0]["where"]
    assert "source=file" in rejections[0]["where"]

    assert len(source.replies) == 1
    trigger, note = source.replies[0]
    assert trigger.external_id == "e1"
    assert "actor_not_allowed" in note


def test_c7_negative_ledger_only_breaks_the_feedback_guarantee(store, monkeypatch):
    """The counter-example: write the refusal to the ledger but never reply."""

    event = make_event("e1", actor="mallory")
    source = ScriptedSource([event])
    loop = Loop(make_app(store, {"s": source}, FakeExecutor()))
    monkeypatch.setattr(loop, "_reply", lambda *args, **kwargs: None)

    loop.pass_once()

    rejections = []
    for record in store.since(None):
        rejections.extend(record.payload.get("rejections", ()) or ())
    assert len(rejections) == 1
    assert source.replies == []
    assert len(source.replies) != len(rejections)  # the C7 assertion would fail
