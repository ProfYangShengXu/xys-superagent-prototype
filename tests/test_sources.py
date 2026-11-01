"""Source adapters: JSONL files and the GitHub command line."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from xys_superagent.adapters.source_file import BadEvent, CursorOutOfRange, FileSource
from xys_superagent.adapters.source_github import (
    GhBadResponse,
    GhFailed,
    GhMissing,
    GithubSource,
    ReplyUnavailable,
)

from conftest import make_event


def _lines(path: Path, events) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_file_source_reads_and_resumes(tmp_path):
    path = tmp_path / "events.jsonl"
    _lines(
        path,
        [
            {
                "external_id": "e1",
                "actor": "alice",
                "text": "one",
                "workspace": "demo",
                "scope_paths": ["src"],
                "mentions_agent": True,
            },
            {
                "external_id": "e2",
                "actor": "bob",
                "text": "two",
                "workspace": "demo",
                "scope_paths": ["docs"],
                "mentions_agent": True,
            },
        ],
    )
    source = FileSource(str(path), source_id="local")
    events, cursor = source.next_batch(None)
    assert [event.external_id for event in events] == ["e1", "e2"]
    assert cursor == path.stat().st_size
    again, cursor2 = source.next_batch(cursor)
    assert again == []
    assert cursor2 == cursor


def test_file_source_reports_bad_lines_without_freezing(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"external_id": "bad", "actor": "alice"}\n'  # missing workspace
        "this is not json\n"
        '{"external_id": "good", "actor": "bob", "text": "ok", "workspace": "demo", "mentions_agent": true}\n',
        encoding="utf-8",
    )
    source = FileSource(str(path), source_id="local")
    events, _ = source.next_batch(None)
    assert [event.external_id for event in events] == ["good"]
    assert len(source.errors) == 2
    assert any("missing field 'workspace'" in error for error in source.errors)


def test_file_source_defaults_to_no_mention(tmp_path):
    path = tmp_path / "events.jsonl"
    _lines(path, [{"external_id": "e1", "actor": "alice", "text": "hi", "workspace": "demo"}])
    event, _ = FileSource(str(path), source_id="local").next_batch(None)
    assert event[0].mentions_agent is False


def test_file_source_missing_file_and_bad_cursor(tmp_path):
    source = FileSource(str(tmp_path / "nope.jsonl"), source_id="local")
    with pytest.raises(BadEvent, match="does not exist"):
        source.next_batch(None)

    path = tmp_path / "events.jsonl"
    _lines(path, [{"external_id": "e1", "actor": "alice", "text": "hi", "workspace": "demo"}])
    with pytest.raises(CursorOutOfRange):
        FileSource(str(path), source_id="local").next_batch(10_000)


def test_file_source_reply_is_durable(tmp_path):
    path = tmp_path / "events.jsonl"
    _lines(path, [{"external_id": "e1", "actor": "alice", "text": "hi", "workspace": "demo"}])
    source = FileSource(str(path), source_id="local")
    source.reply(make_event("e1", source_id="local"), "refused: not_explicit_request")
    reply_lines = (tmp_path / "events.jsonl.replies.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(reply_lines) == 1
    assert "not_explicit_request" in json.loads(reply_lines[0])["note"]


# -- GitHub source -------------------------------------------------------


def _runner(payload, *, returncode=0, stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if isinstance(payload, str):
            stdout = payload
        else:
            stdout = json.dumps(payload)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_github_missing_tool_raises_with_install_hint():
    with pytest.raises(GhMissing) as caught:
        GithubSource(which=lambda name: None)
    assert "install" in str(caught.value).lower()


def test_github_issue_becomes_a_trigger():
    payload = [
        {
            "id": "I_1",
            "number": 7,
            "title": "parser crashes",
            "body": "steps to reproduce",
            "author": {"login": "alice"},
            "url": "https://example.invalid/7",
            "updatedAt": "2026-01-02T03:04:05Z",
        }
    ]
    source = GithubSource(
        repo="acme/demo",
        which=lambda name: "/usr/bin/gh",
        runner=_runner(payload),
    )
    events, cursor = source.next_batch(None)
    assert len(events) == 1
    assert events[0].external_id == "acme/demo#7"
    assert events[0].actor == "alice"
    assert "parser crashes" in events[0].text
    assert cursor == "2026-01-02T03:04:05Z"

    again, _ = source.next_batch(cursor)
    assert again == []


def test_github_command_failure_is_loud():
    source = GithubSource(
        repo="acme/demo",
        which=lambda name: "/usr/bin/gh",
        runner=_runner([], returncode=1, stderr="boom"),
    )
    with pytest.raises(GhFailed, match="boom"):
        source.next_batch(None)


def test_github_garbage_output_is_loud():
    source = GithubSource(
        repo="acme/demo",
        which=lambda name: "/usr/bin/gh",
        runner=_runner("not json"),
    )
    with pytest.raises(GhBadResponse):
        source.next_batch(None)


def test_github_reply_requires_a_repository():
    source = GithubSource(which=lambda name: "/usr/bin/gh", runner=_runner([]))
    with pytest.raises(ReplyUnavailable, match="repo"):
        source.reply(make_event("n1", source_id="github"), "refused")


def test_github_reply_comments_on_the_issue():
    runner = _runner([])
    source = GithubSource(repo="acme/demo", which=lambda name: "/usr/bin/gh", runner=runner)
    source.reply(make_event("acme/demo#7", source_id="github"), "refused: actor_not_allowed")
    command = runner.calls[-1]  # type: ignore[attr-defined]
    assert command[:3] == ["/usr/bin/gh", "issue", "comment"]
    assert "acme/demo#7" in command
    assert any("refused: actor_not_allowed" in part for part in command)
