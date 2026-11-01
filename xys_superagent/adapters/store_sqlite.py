"""SQLite ledger adapter.

Two facts drive the schema:

* one unique index on ``(source_id, external_id)`` makes redelivery a database
  question instead of an ``if`` in the loop;
* a pass record carries the new cursor *and* the tickets it produced, so both
  land in one transaction.  Delivery to an executor happens only after that
  commit: the worst case is "consumed but not dispatched" (replayable), never
  "dispatched but unrecorded" (duplicate work).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

try:  # POSIX only; the single-writer rule is enforced where it can be
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from ..core.types import Cursor, Record

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL,
    at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS triggers (
    source_id   TEXT NOT NULL,
    external_id TEXT NOT NULL,
    key         TEXT NOT NULL,
    ticket_id   TEXT,
    actor       TEXT NOT NULL DEFAULT '',
    actor_kind  TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL,
    at          REAL NOT NULL,
    PRIMARY KEY (source_id, external_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_triggers_source_external
    ON triggers (source_id, external_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_triggers_key ON triggers (key);
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    key         TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    external_id TEXT NOT NULL,
    workspace   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    at          REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_key ON tickets (key);
CREATE TABLE IF NOT EXISTS cursors (
    source_id TEXT PRIMARY KEY,
    value     TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL,
    external_id TEXT NOT NULL,
    reason     TEXT NOT NULL,
    where_text TEXT NOT NULL,
    at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handbacks (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    reason    TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    """The ledger refused or failed an operation."""


def _warn(message: str) -> None:
    print(f"xys_superagent: {message}", file=sys.stderr)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class SqliteStore:
    """Single-writer ledger.  One process owns one database file."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        self._lock_handle: Any = None
        if self.path != ":memory:":
            self._take_lock()
        try:
            self._conn = sqlite3.connect(
                self.path,
                timeout=busy_timeout_ms / 1000.0,
                isolation_level=None,  # explicit transactions only
            )
        except sqlite3.Error as exc:  # pragma: no cover - filesystem failure
            raise StoreError(f"cannot open ledger {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            mode = str(
                self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).lower()
        except sqlite3.OperationalError as exc:
            raise StoreError(
                f"cannot enable WAL for {self.path}: {exc}. "
                "Move the ledger to an ext4 path (for example a directory under "
                "the WSL home) instead of a Windows drive."
            ) from exc
        if mode != "wal":
            _warn(
                f"WAL was refused by the filesystem for {self.path} (mode={mode}); "
                "using the rollback journal. This is safe but slower; an ext4 path "
                "is recommended for the ledger."
            )
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT value FROM meta WHERE name='schema_version'"
        ).fetchone()
        if row is None:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO meta(name, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        elif int(row["value"]) != SCHEMA_VERSION:
            raise StoreError(
                f"ledger schema version {row['value']} != expected {SCHEMA_VERSION}"
            )

    # -- plumbing ---------------------------------------------------------

    def _take_lock(self) -> None:
        """Refuse to open a ledger someone else is writing.

        SQLite itself allows many connections; the system it serves does not.
        A lock file makes "one process per ledger" a hard rule instead of a
        sentence in a design document.
        """

        if fcntl is None:  # pragma: no cover - Windows
            return
        lock_path = self.path + ".lock"
        try:
            handle = open(lock_path, "a+", encoding="utf-8")
        except OSError as exc:
            raise StoreError(f"cannot create lock file {lock_path}: {exc}") from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise StoreError(
                f"ledger {self.path} is already open in another process "
                f"(lock file {lock_path}). xys_superagent is single-writer; "
                "stop the other process (a long-running 'xys_superagent run') first."
            ) from exc
        self._lock_handle = handle

    @contextmanager
    def _tx(self) -> Iterator[None]:
        # BEGIN IMMEDIATE: a deferred transaction that reads first and then
        # writes fails with SQLITE_BUSY immediately, and busy_timeout does not
        # help once a read transaction must be upgraded.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        if self._lock_handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                self._lock_handle.close()
            except OSError:
                pass
            self._lock_handle = None

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- StorePort ---------------------------------------------------------

    def append(self, record: Record) -> Cursor:
        payload = dict(record.payload)
        try:
            with self._tx():
                cursor = self._conn.execute(
                    "INSERT INTO records(key, kind, payload, at) VALUES(?,?,?,?)",
                    (record.key, record.kind, _dumps(payload), time.time()),
                ).lastrowid
                self._apply(record.kind, record.key, payload)
            return int(cursor)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise StoreError(f"append({record.kind}:{record.key}) failed: {exc}") from exc

    def since(self, cursor: Cursor | None) -> list[Record]:
        start = int(cursor) if cursor is not None else 0
        rows = self._conn.execute(
            "SELECT key, kind, payload FROM records WHERE seq > ? ORDER BY seq",
            (start,),
        ).fetchall()
        return [
            Record(key=row["key"], kind=row["kind"], payload=json.loads(row["payload"]))
            for row in rows
        ]

    def find(self, key: str) -> Record | None:
        row = self._conn.execute(
            "SELECT key, payload FROM triggers WHERE key = ? LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            return Record(key=row["key"], kind="trigger", payload=json.loads(row["payload"]))
        row = self._conn.execute(
            "SELECT id, payload FROM tickets WHERE id = ? OR key = ? LIMIT 1",
            (key, key),
        ).fetchone()
        if row is not None:
            return Record(key=row["id"], kind="ticket", payload=json.loads(row["payload"]))
        if key.startswith("cursor:"):
            row = self._conn.execute(
                "SELECT value FROM cursors WHERE source_id = ?",
                (key[len("cursor:"):],),
            ).fetchone()
            if row is not None:
                return Record(
                    key=key,
                    kind="cursor",
                    payload={"source_id": key[len("cursor:"):], "value": json.loads(row["value"])},
                )
        row = self._conn.execute(
            "SELECT key, kind, payload FROM records WHERE key = ? ORDER BY seq DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            return Record(key=row["key"], kind=row["kind"], payload=json.loads(row["payload"]))
        return None

    # -- extra read helpers used by the app (not part of the port) ---------

    def count_tickets(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0])

    def count_records(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def duplicate_trigger_rows(self) -> int:
        """Rows sharing (source_id, external_id); must always be 0."""

        row = self._conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT source_id, external_id FROM triggers"
            "  GROUP BY source_id, external_id HAVING COUNT(*) > 1)"
        ).fetchone()
        return int(row[0])

    # -- decomposition of a record into indexed tables ---------------------

    def _apply(self, kind: str, key: str, payload: Mapping[str, Any]) -> None:
        if kind in ("pass", "batch"):
            for item in payload.get("triggers", ()) or ():
                self._insert_trigger(item)
            for ticket in payload.get("tickets", ()) or ():
                self._upsert_ticket(ticket)
            for rejection in payload.get("rejections", ()) or ():
                self._insert_rejection(rejection)
            for handback in payload.get("handbacks", ()) or ():
                self._insert_handback(handback)
            for source_id, value in (payload.get("cursor", {}) or {}).items():
                self._set_cursor(str(source_id), value)
        elif kind == "trigger":
            self._insert_trigger(payload)
        elif kind == "ticket":
            self._upsert_ticket(payload)
        elif kind == "cursor":
            source_id = str(payload.get("source_id", ""))
            self._set_cursor(source_id, payload.get("value"))
        elif kind == "rejection":
            self._insert_rejection(payload)
        elif kind == "handback":
            self._insert_handback(payload)
        # other kinds (errors, notes) live in `records` only

    def _insert_trigger(self, item: Mapping[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO triggers"
            "(source_id, external_id, key, ticket_id, actor, actor_kind, payload, at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                str(item.get("source_id", "")),
                str(item.get("external_id", "")),
                str(item.get("key", "")),
                item.get("ticket_id"),
                str(item.get("actor", "")),
                str(item.get("actor_kind", "")),
                _dumps(dict(item)),
                float(item.get("created_at", 0.0) or 0.0),
            ),
        )

    def _upsert_ticket(self, item: Mapping[str, Any]) -> None:
        # A ticket without an id or a key cannot be addressed later.  Fail as a
        # StoreError (not a bare KeyError) so the caller rolls the whole pass
        # back instead of leaving half of it on disk.
        for required in ("id", "key"):
            if not str(item.get(required) or "").strip():
                raise StoreError(
                    f"ticket record is missing '{required}': {sorted(item.keys())}"
                )
        self._conn.execute(
            "INSERT INTO tickets(id, key, source_id, external_id, workspace, payload, at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, at=excluded.at",
            (
                str(item["id"]),
                str(item["key"]),
                str(item.get("source_id", "")),
                str(item.get("external_id", "")),
                str(item.get("workspace", "")),
                _dumps(dict(item)),
                float(item.get("created_at", 0.0) or 0.0),
            ),
        )

    def _insert_rejection(self, item: Mapping[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO rejections(source_id, external_id, reason, where_text, at)"
            " VALUES(?,?,?,?,?)",
            (
                str(item.get("source_id", "")),
                str(item.get("external_id", "")),
                str(item.get("reason", "")),
                str(item.get("where", "")),
                float(item.get("at", 0.0) or 0.0),
            ),
        )

    def _insert_handback(self, item: Mapping[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO handbacks(ticket_id, reason, at) VALUES(?,?,?)",
            (
                str(item.get("ticket_id", "")),
                str(item.get("reason", "")),
                float(item.get("at", 0.0) or 0.0),
            ),
        )

    def _set_cursor(self, source_id: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO cursors(source_id, value, at) VALUES(?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET value=excluded.value, at=excluded.at",
            (source_id, _dumps(value), 0.0),
        )
