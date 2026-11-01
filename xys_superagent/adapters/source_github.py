"""Read issues or notifications through the GitHub command line.

If the command line tool is absent this adapter raises :class:`GhMissing` and
says how to install it.  It never returns an empty batch to pretend that
nothing happened: "no events" and "cannot see events" are different facts, and
confusing them is how a queue silently stops.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from ..core.types import Cursor, Trigger, WorkRef

INSTALL_HINT = (
    "install the GitHub CLI: https://cli.github.com/ "
    "(Debian/Ubuntu: 'sudo apt install gh'; macOS: 'brew install gh'), "
    "then run 'gh auth login' once. "
    "Alternatively set [source.<name>] kind='file' to use a local event file."
)


class GhMissing(RuntimeError):
    """The GitHub command line tool is not installed."""


class GhFailed(RuntimeError):
    """The command line tool ran but returned an error."""


class GhBadResponse(RuntimeError):
    """The command line tool returned something that is not a usable event."""


class ReplyUnavailable(RuntimeError):
    """This source cannot carry a reply back to the conversation."""


class GithubSource:
    """Issues of one repository, or the account's notifications."""

    source_id = "github"

    def __init__(
        self,
        *,
        repo: str = "",
        batch_size: int = 30,
        workspace: str = "",
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_s: float = 30.0,
    ) -> None:
        self.repo = repo
        self.batch_size = batch_size
        self.workspace = workspace or repo or "github"
        self._runner = runner
        self._timeout = timeout_s
        self.gh = which("gh")
        if not self.gh:
            raise GhMissing(f"the 'gh' command was not found on PATH. {INSTALL_HINT}")

    # -- SourcePort ---------------------------------------------------------

    def next_batch(self, cursor: Cursor | None) -> tuple[list[Trigger], Cursor]:
        since = "" if cursor is None else str(cursor)
        if self.repo:
            items = self._run_json(
                [
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--state",
                    "open",
                    "--limit",
                    str(self.batch_size),
                    "--json",
                    "id,number,title,body,author,url,updatedAt",
                ]
            )
        else:
            items = self._run_json(
                ["api", f"notifications?per_page={self.batch_size}"],
            )
        if not isinstance(items, list):
            raise GhBadResponse(
                f"expected a JSON array from gh, got {type(items).__name__}"
            )
        events: list[Trigger] = []
        newest = since
        for item in items:
            if not isinstance(item, Mapping):
                raise GhBadResponse("an entry from gh is not a JSON object")
            updated = str(item.get("updatedAt") or item.get("updated_at") or "")
            if since and updated and updated <= since:
                continue
            events.append(self._to_trigger(item))
            if updated > newest:
                newest = updated
        return events, newest

    def acknowledge(self, cursor: Cursor) -> None:
        return None

    def reply(self, trigger: Trigger, note: str) -> None:
        if not self.repo:
            raise ReplyUnavailable(
                "this source reads the account's notifications; set [source.<name>] repo "
                "so a refused request can be answered on the issue itself"
            )
        body = f"> xys_superagent: {note}"
        self._run(["issue", "comment", str(trigger.external_id), "--repo", self.repo, "--body", body])

    # -- internals -----------------------------------------------------------

    def _run(self, argv: Sequence[str]) -> str:
        assert self.gh is not None
        command = [self.gh, *argv]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:  # pragma: no cover - race with PATH
            raise GhMissing(f"'{self.gh}' disappeared. {INSTALL_HINT}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhFailed(f"gh timed out after {self._timeout}s: {' '.join(argv)}") from exc
        if completed.returncode != 0:
            raise GhFailed(
                f"gh exited {completed.returncode}: {' '.join(argv)}\n"
                f"stderr: {(completed.stderr or '').strip()[:400]}"
            )
        return completed.stdout or ""

    def _run_json(self, argv: Sequence[str]) -> Any:
        raw = self._run(argv)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GhBadResponse(f"gh output is not JSON ({exc}): {raw[:200]!r}") from exc

    def _to_trigger(self, item: Mapping[str, Any]) -> Trigger:
        number = item.get("number")
        if number is not None:
            external_id = f"{self.repo}#{number}" if self.repo else str(item.get("id", number))
            title = str(item.get("title", ""))
            body = str(item.get("body") or "")
            author = item.get("author") or {}
            login = str(author.get("login", "")) if isinstance(author, Mapping) else ""
            text = (title + "\n\n" + body).strip()
            thread = str(item.get("url", ""))
        else:
            external_id = str(item.get("id", ""))
            subject = item.get("subject") or {}
            repository = item.get("repository") or {}
            title = str(subject.get("title", "")) if isinstance(subject, Mapping) else ""
            login = ""
            text = title
            thread = str(repository.get("full_name", "")) if isinstance(repository, Mapping) else ""
        if not external_id:
            raise GhBadResponse(f"event without an identifier: {dict(item)!r}")
        actor_kind = "bot" if login.endswith("[bot]") or login.endswith("-bot") else "person"
        return Trigger(
            source_id=self.source_id,
            external_id=external_id,
            actor=login,
            actor_kind=actor_kind,
            text=text,
            work=WorkRef(
                workspace=self.workspace,
                scope_paths=(),
                summary=title[:120],
            ),
            mentions_agent=True,
            thread=thread,
            created_at=time.time(),
        )
