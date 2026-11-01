"""Admission: may this event start work?

Three outcomes only: Admit, Candidate, Reject(reason).  A message is an input
to the decision; it cannot change the policy.
"""

from __future__ import annotations

from .ticket import covers_path
from .types import (
    BOT,
    DECISION_ADMIT,
    DECISION_CANDIDATE,
    DECISION_REJECT,
    PERSON,
    Decision,
    Policy,
    Trigger,
)


def _where(trigger: Trigger) -> str:
    thread = trigger.thread or "-"
    return f"source={trigger.source_id} thread={thread} actor={trigger.actor}"


def admit(trigger: Trigger, policy: Policy) -> Decision:
    """Decide one trigger against the team policy.

    ``where`` is filled for every outcome, not only rejections: a decision
    without a location cannot be acted on.
    """

    where = _where(trigger)

    if trigger.actor_kind not in (PERSON, BOT):
        return Decision(DECISION_REJECT, "unknown_actor_kind", where)

    if trigger.actor_kind == BOT:
        if trigger.actor not in policy.allowed_bots:
            return Decision(DECISION_REJECT, "bot_not_allowed", where)
        # A bot may produce a candidate; it may never produce a firm request.
        return Decision(DECISION_CANDIDATE, "bot_can_only_propose", where)

    if trigger.actor not in policy.allowed_people:
        return Decision(DECISION_REJECT, "actor_not_allowed", where)
    if not trigger.mentions_agent:
        return Decision(DECISION_REJECT, "not_explicit_request", where)
    if not trigger.text.strip():
        return Decision(DECISION_REJECT, "empty_request", where)

    requested = trigger.work.scope_paths or policy.default_scope_paths
    if any(not isinstance(path, str) or not path.strip() for path in requested):
        return Decision(DECISION_REJECT, "empty_scope", where)

    if policy.allowed_scope_roots:
        for path in requested:
            if not any(
                covers_path(root, path) for root in policy.allowed_scope_roots
            ):
                return Decision(
                    DECISION_REJECT,
                    f"scope_outside_policy:{path or '.'}",
                    where,
                )

    return Decision(DECISION_ADMIT, "", where)
