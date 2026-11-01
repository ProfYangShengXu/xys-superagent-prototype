"""Identity of an event.

The idempotency key is the whole deduplication mechanism: the same event
delivered twice must map to the same string, therefore to one ticket.
"""

from __future__ import annotations


def trigger_key(source_id: str, external_id: str) -> str:
    """Return ``source_id:external_id``.

    Both parts must be non-empty strings.  A key that silently degrades to a
    random or timestamped value would turn every redelivery into a new ticket,
    so the inputs are validated instead of coerced.
    """

    for name, value in (("source_id", source_id), ("external_id", external_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string, got {type(value).__name__}")
        if not value.strip():
            raise ValueError(f"{name} must not be empty")
        # A ':' inside a part would let two different pairs collide on one key
        # ("a:b"+"c" and "a"+"b:c"), which turns idempotency into data loss.
        if ":" in value:
            raise ValueError(f"{name} must not contain ':' (it separates the two parts)")
    return f"{source_id}:{external_id}"
