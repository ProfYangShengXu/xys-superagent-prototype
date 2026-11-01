"""End to end: example events, restart, the CLI and the two-second budget."""

from __future__ import annotations

import json
import time
from io import StringIO

from xys_superagent.adapters.panel_text import TextPanel
from xys_superagent.app.build import build_app
from xys_superagent.app.cli import main
from xys_superagent.app.loop import Loop
from xys_superagent.core.ticket import Mandate, Ticket
from xys_superagent.core.types import Record

from conftest import FakeExecutor, ROOT, ScriptedSource, make_app


def _config(tmp_path, database: str) -> dict:
    return {
        "app": {"database": database, "batch_size": 10},
        "policy": {
            "allowed_people": ["alice", "bob"],
            "allowed_bots": ["ci-bot"],
            "max_rounds": 3,
        },
        "sources": {
            "local": {
                "kind": "file",
                "path": str(ROOT / "examples" / "events.jsonl"),
                "workspace": "demo",
            }
        },
        "executor": {
            "kind": "cli",
            "command": ["sh", "-c", "echo accepted: $XYS_SUMMARY"],
        },
        "panel": {"kind": "text"},
    }


def _toml(tmp_path, database: str) -> str:
    events = ROOT / "examples" / "events.jsonl"
    text = f"""
[app]
database = "{database}"
batch_size = 10

[policy]
allowed_people = ["alice", "bob"]
allowed_bots = ["ci-bot"]
max_rounds = 3

[sources.local]
kind = "file"
path = "{events}"
workspace = "demo"

[executor]
kind = "cli"
command = ["sh", "-c", "echo accepted: $XYS_SUMMARY"]

[panel]
kind = "text"
"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_example_runs_four_passes_and_lands_in_review(tmp_path):
    app = build_app(_config(tmp_path, str(tmp_path / "e2e.db")))
    buffer = StringIO()
    app.panel = TextPanel(buffer)
    try:
        loop = Loop(app)
        for _ in range(4):
            loop.pass_once()
        assert app.store.count_tickets() == 2
        lines = buffer.getvalue().strip().splitlines()
        assert len(lines) == 2
        assert all("waiting_review" in line for line in lines)
        assert all("xys_superagent ticket" in line for line in lines)
    finally:
        app.close()


def test_restart_replays_nothing(tmp_path):
    config = _config(tmp_path, str(tmp_path / "restart.db"))
    first = build_app(config)
    try:
        loop = Loop(first)
        for _ in range(4):
            loop.pass_once()
        assert first.store.count_tickets() == 2
        assert first.store.find("cursor:local") is not None
    finally:
        first.close()

    second = build_app(config)
    try:
        stats = Loop(second).pass_once()
        assert stats.tickets_created == 0
        assert second.store.count_tickets() == 2
    finally:
        second.close()


def test_cli_view_ticket_and_run(tmp_path, capsys):
    database = str(tmp_path / "cli.db")
    config = _toml(tmp_path, database)

    app = build_app(config)
    buffer = StringIO()
    app.panel = TextPanel(buffer)
    try:
        loop = Loop(app)
        for _ in range(4):
            loop.pass_once()
        assert app.store.count_tickets() == 2
    finally:
        app.close()

    assert main(["view", "--config", config]) == 0
    view = capsys.readouterr().out
    assert len(view.strip().splitlines()) == 2

    app = build_app(config)
    try:
        first_id = sorted(t.id for t in list(Loop(app).tickets.values()))[0]
    finally:
        app.close()

    assert main(["ticket", first_id, "--config", config]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == first_id
    assert payload["mandate"]["scope_paths"]

    assert main(["run", "--config", config, "--once"]) == 0


def test_cli_doctor_reports_every_check(tmp_path, capsys):
    config = _toml(tmp_path, str(tmp_path / "doctor.db"))
    code = main(["doctor", "--config", config])
    output = capsys.readouterr().out
    assert code == 0, output
    assert "OK    python" in output or "OK    python:" in output
    assert "layering" in output
    assert "FAIL" not in output


def test_two_hundred_tickets_fit_in_two_seconds(store):
    tickets = []
    for index in range(200):
        ticket = Ticket(
            id=f"perf{index:03d}",
            key=f"file:perf{index}",
            source_id="scripted",
            external_id=f"perf{index}",
            workspace=f"ws{index}",
            mandate=Mandate(scope_paths=("src",)),
            work_summary=f"job {index}",
            created_at=1000.0 + index,
        )
        tickets.append(ticket.to_dict())
    store.append(Record("seed", "batch", {"tickets": tickets}))

    app = make_app(store, {"s": ScriptedSource([])}, FakeExecutor())
    loop = Loop(app)
    assert len(loop.tickets) == 200

    started = time.perf_counter()
    stats = loop.pass_once()
    elapsed = time.perf_counter() - started

    assert stats.dispatched == 200
    assert elapsed < 2.0, f"one pass over 200 tickets took {elapsed:.2f}s"
