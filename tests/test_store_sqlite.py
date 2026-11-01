"""The SQLite ledger: uniqueness, atomicity, single writer."""

from __future__ import annotations

import pytest

from xys_superagent.adapters.store_sqlite import SqliteStore, StoreError
from xys_superagent.core.types import Record


def _pass_payload(**overrides):
    payload = {
        "cursor": {"file": 12},
        "triggers": [
            {
                "source_id": "file",
                "external_id": "e1",
                "key": "file:e1",
                "ticket_id": "t1",
                "actor": "alice",
                "actor_kind": "person",
                "created_at": 1.0,
            }
        ],
        "tickets": [
            {
                "id": "t1",
                "key": "file:e1",
                "source_id": "file",
                "external_id": "e1",
                "workspace": "demo",
                "mandate": {"scope_paths": ["src"], "allowed_actions": ["read", "write"], "max_rounds": 3},
                "created_at": 1.0,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_unique_index_on_source_and_external_id(store):
    store.append(Record("pass:1", "pass", _pass_payload()))
    # A redelivery with a different key must still not create a second trigger.
    payload = _pass_payload(
        triggers=[
            {
                "source_id": "file",
                "external_id": "e1",
                "key": "file:e1:retry",
                "ticket_id": "t2",
                "actor": "alice",
                "actor_kind": "person",
            }
        ]
    )
    store.append(Record("pass:2", "pass", payload))
    assert store.duplicate_trigger_rows() == 0
    rows = store._conn.execute("SELECT COUNT(*) FROM triggers").fetchone()[0]
    assert rows == 1


def test_cursor_and_ticket_commit_together(store):
    store.append(Record("pass:1", "pass", _pass_payload()))
    found = store.find("cursor:file")
    assert found is not None
    assert found.payload["value"] == 12
    assert store.count_tickets() == 1


def test_bad_ticket_rolls_back_the_whole_pass(store):
    payload = _pass_payload()
    payload["tickets"] = [
        payload["tickets"][0],
        {"key": "no-id"},  # invalid: no id
    ]
    with pytest.raises(StoreError):
        store.append(Record("pass:bad", "pass", payload))
    assert store.count_records() == 0
    assert store.count_tickets() == 0
    assert store.find("cursor:file") is None


def test_second_writer_is_refused(tmp_path):
    path = str(tmp_path / "ledger.db")
    first = SqliteStore(path)
    try:
        with pytest.raises(StoreError, match="already open"):
            SqliteStore(path)
    finally:
        first.close()
    second = SqliteStore(path)
    second.close()


def test_since_returns_records_in_order(store):
    first = store.append(Record("a", "note", {"n": 1}))
    store.append(Record("b", "note", {"n": 2}))
    tail = store.since(first)
    assert [record.key for record in tail] == ["b"]
    assert [record.key for record in store.since(None)] == ["a", "b"]


def test_wal_is_enabled(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
