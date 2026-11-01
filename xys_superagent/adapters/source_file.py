"""Read events from a local JSONL file.

This is the source used by the runnable example, and it is also the reference
for what a source looks like.  The cursor is a byte offset, so a restart
resumes exactly where the previous pass stopped.

One line is one event::

    {"external_id": "e1", "actor": "alice", "actor_kind": "person",
     "text": "please fix the parser", "workspace": "demo",
     "scope_paths": ["src/parser"], "mentions_agent": true}
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

from ..core.types import Cursor, Trigger, WorkRef


class BadEvent(ValueError):
    """A line in the event file is not a usable event."""


class CursorOutOfRange(ValueError):
    """The stored read position is past the end of the file."""


def _text(value: Any, *, field: str, where: str, required: bool = True) -> str:
    if value is None:
        if required:
            raise BadEvent(f"{where}: missing field {field!r}")
        return ""
    if not isinstance(value, str):
        raise BadEvent(f"{where}: field {field!r} must be a string")
    return value


class FileSource:
    """A source backed by one JSONL file."""

    def __init__(
        self,
        path: str,
        *,
        source_id: str = "",
        batch_size: int = 50,
        default_workspace: str = "",
    ) -> None:
        self.path = str(path)
        self.source_id = source_id or os.path.basename(self.path)
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size
        self.default_workspace = default_workspace
        self.replies_path = self.path + ".replies.jsonl"
        self._last_ack: Cursor | None = None
        #: Malformed lines seen by the last ``next_batch``.  The batch continues;
        #: the app turns these into durable error records so a single bad line
        #: cannot freeze the stream forever, and cannot vanish silently either.
        self.errors: list[str] = []

    # -- SourcePort ---------------------------------------------------------

    def next_batch(self, cursor: Cursor | None) -> tuple[list[Trigger], Cursor]:
        offset = 0 if cursor is None else self._as_offset(cursor)
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            raise BadEvent(f"event file does not exist: {self.path}") from None
        if offset > size:
            raise CursorOutOfRange(
                f"cursor {offset} is past end of {self.path} ({size} bytes); "
                "the file was truncated or replaced while the ledger remembers the old position"
            )

        events: list[Trigger] = []
        with open(self.path, "rb") as handle:
            handle.seek(offset)
            line_no = 0
            while len(events) < self.batch_size:
                raw = handle.readline()
                if not raw:
                    break
                line_no += 1
                if not raw.strip():
                    offset += len(raw)
                    continue
                where = f"{self.path}:byte {offset}"
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    events.append(self._to_trigger(obj, where=where))
                except (UnicodeDecodeError, json.JSONDecodeError, BadEvent) as exc:
                    self.errors.append(f"{where}: {exc}")
                offset += len(raw)
        return events, offset

    def acknowledge(self, cursor: Cursor) -> None:
        self._last_ack = cursor

    def reply(self, trigger: Trigger, note: str) -> None:
        """Append a note next to the event file.

        The reply is a real, durable line rather than a log message: the point
        of the authorship rule is that the person who asked can read the answer.
        """

        record = {
            "replies_to": trigger.external_id,
            "thread": trigger.thread,
            "note": note,
            "at": time.time(),
        }
        with open(self.replies_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _as_offset(cursor: Cursor) -> int:
        try:
            value = int(cursor)
        except (TypeError, ValueError) as exc:
            raise BadEvent(f"cursor must be a byte offset, got {cursor!r}") from exc
        if value < 0:
            raise BadEvent(f"cursor must be >= 0, got {value}")
        return value

    def _to_trigger(self, obj: Any, *, where: str) -> Trigger:
        if not isinstance(obj, Mapping):
            raise BadEvent(f"{where}: an event must be a JSON object")
        external_id = _text(obj.get("external_id"), field="external_id", where=where)
        text = _text(obj.get("text"), field="text", where=where, required=False)
        actor = _text(obj.get("actor"), field="actor", where=where)
        actor_kind = _text(
            obj.get("actor_kind"), field="actor_kind", where=where, required=False
        ) or "person"
        scope = obj.get("scope_paths", ())
        if isinstance(scope, str):
            scope = [scope]
        if not isinstance(scope, (list, tuple)) or not all(
            isinstance(item, str) for item in scope
        ):
            raise BadEvent(f"{where}: scope_paths must be a list of strings")
        workspace = _text(
            obj.get("workspace"), field="workspace", where=where, required=False
        ) or self.default_workspace
        if not workspace:
            raise BadEvent(f"{where}: missing field 'workspace' (no default given)")
        mentions = obj.get("mentions_agent", False)
        if not isinstance(mentions, bool):
            raise BadEvent(f"{where}: mentions_agent must be a boolean")
        created = obj.get("created_at", 0.0)
        if not isinstance(created, (int, float)):
            raise BadEvent(f"{where}: created_at must be a number")
        try:
            return Trigger(
                source_id=self.source_id,
                external_id=external_id,
                actor=actor,
                actor_kind=actor_kind,
                text=text,
                work=WorkRef(
                    workspace=workspace,
                    branch=_text(
                        obj.get("branch"), field="branch", where=where, required=False
                    ),
                    scope_paths=tuple(scope),
                    summary=_text(
                        obj.get("summary"), field="summary", where=where, required=False
                    )
                    or text[:120],
                ),
                mentions_agent=mentions,
                thread=_text(
                    obj.get("thread"), field="thread", where=where, required=False
                ),
                created_at=float(created),
            )
        except ValueError as exc:
            raise BadEvent(f"{where}: {exc}") from exc
