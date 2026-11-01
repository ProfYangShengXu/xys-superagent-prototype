"""Command line entry point: run, view, ticket, doctor."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..adapters.panel_text import TextPanel
from ..core.panel import attention_view
from .build import App, ConfigError, build_app, load_config, open_store
from .loop import Loop, replay_tickets

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xys_superagent",
        description="Ticket orchestration across agent frameworks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pass loop")
    run.add_argument("--config", "-c", required=True, help="path to a TOML config")
    run.add_argument("--once", action="store_true", help="run a single pass")
    run.add_argument("--max-passes", type=int, default=None, help="stop after N passes")
    run.add_argument("--interval", type=float, default=None, help="seconds between passes")

    view = sub.add_parser("view", help="print the attention panel and exit")
    view.add_argument("--config", "-c", required=True, help="path to a TOML config")

    ticket = sub.add_parser("ticket", help="print one ticket as JSON")
    ticket.add_argument("id", help="ticket id (short name from the panel)")
    ticket.add_argument("--config", "-c", required=True, help="path to a TOML config")

    doctor = sub.add_parser("doctor", help="check the environment and the wiring")
    doctor.add_argument("--config", "-c", required=True, help="path to a TOML config")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "view":
            return _view(args)
        if args.command == "ticket":
            return _ticket(args)
        if args.command == "doctor":
            return _doctor(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return 130
    return EXIT_FAILURE


def _run(args: argparse.Namespace) -> int:
    app = build_app(args.config)
    loop = Loop(app)

    def on_pass(stats: Any, number: int) -> None:
        if stats.quiet:
            return
        print(
            f"pass {number}: fetched={stats.fetched} duplicates={stats.duplicate_triggers} "
            f"tickets+={stats.tickets_created} dispatched={stats.dispatched} "
            f"reports={stats.reports} rejected={stats.rejected} "
            f"handbacks={stats.handbacks} waiting={stats.waited} errors={stats.errors}",
            file=sys.stderr,
        )

    max_passes = 1 if args.once else args.max_passes
    try:
        loop.run_forever(
            interval_s=args.interval,
            max_passes=max_passes,
            on_pass=on_pass,
        )
    finally:
        app.close()
    return EXIT_OK


def _view(args: argparse.Namespace) -> int:
    store = open_store(args.config)
    try:
        tickets = replay_tickets(store)
    finally:
        store.close()
    view = attention_view([ticket.view() for ticket in tickets.values()])
    TextPanel().publish(view)
    return EXIT_OK


def _ticket(args: argparse.Namespace) -> int:
    store = open_store(args.config)
    try:
        tickets = replay_tickets(store)
        ticket = tickets.get(args.id)
        if ticket is None:
            ticket = next((t for t in tickets.values() if t.key == args.id), None)
    finally:
        store.close()
    if ticket is None:
        print(f"no such ticket: {args.id}", file=sys.stderr)
        return EXIT_FAILURE
    print(json.dumps(ticket.to_dict(), indent=2, ensure_ascii=False))
    return EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []
    app: App | None = None
    cfg: dict[str, Any] | None = None

    def check(name: str, action: Any) -> Any:
        try:
            detail = action()
        except Exception as exc:  # noqa: BLE001 - the point is to report, not raise
            results.append(("FAIL", name, f"{type(exc).__name__}: {exc}"))
            return None
        results.append(("OK", name, str(detail)))
        return detail

    def skip(name: str, reason: str) -> None:
        results.append(("SKIP", name, reason))

    def python_check() -> str:
        if sys.version_info < (3, 11):
            raise ConfigError(f"Python 3.11+ is required, this is {sys.version.split()[0]}")
        return f"{sys.version.split()[0]} at {sys.executable}"

    check("python", python_check)

    def load() -> str:
        nonlocal cfg
        cfg = load_config(args.config)
        return f"parsed {args.config}"

    check("config", load)

    def assemble() -> str:
        nonlocal app
        assert cfg is not None
        app = build_app(cfg)
        return (
            f"sources={sorted(app.sources)} "
            f"executor={type(app.executor).__name__} "
            f"ledger={getattr(app.store, 'path', '?')}"
        )

    if cfg is not None:
        check("assembly", assemble)

    if app is not None:
        for name in sorted(app.sources):
            check(f"source:{name}", lambda name=name: _check_source(app.sources[name]))
        check("executor", lambda: _check_executor(app.executor))
        check("ledger", lambda: _check_ledger(app.store))
        app.close()
    elif cfg is not None:

        def ledger_alone() -> str:
            store = open_store(cfg)
            try:
                return _check_ledger(store)
            finally:
                store.close()

        check("ledger", ledger_alone)

    def layering() -> str:
        script = Path(__file__).resolve().parents[2] / "scripts" / "check_layering.py"
        if not script.is_file():
            raise ConfigError(f"layering check script not found at {script}")
        return _run_script(script, Path(__file__).resolve().parents[2])

    if Path(__file__).resolve().parents[2].joinpath("scripts", "check_layering.py").is_file():
        check("layering", layering)
    else:
        skip("layering", "scripts/check_layering.py is not installed next to the package")

    for status, name, detail in results:
        print(f"{status:5} {name}: {detail}")
    return EXIT_OK if all(status != "FAIL" for status, _, _ in results) else EXIT_FAILURE


def _check_source(source: Any) -> str:
    path = getattr(source, "path", None)
    description = f"{type(source).__name__}"
    if path is not None:
        if not Path(path).is_file():
            raise ConfigError(f"event file does not exist: {path}")
        size = Path(path).stat().st_size
        description += f" path={path} bytes={size}"
    repo = getattr(source, "repo", "")
    if repo:
        description += f" repo={repo}"
    return description


def _check_executor(executor: Any) -> str:
    command = getattr(executor, "command", None)
    if command:
        found = shutil.which(command[0]) or (Path(command[0]).is_file() and command[0])
        if not found:
            raise ConfigError(f"executor command not found: {command[0]!r}")
        return f"{type(executor).__name__} command={command!r}"
    ping = getattr(executor, "ping", None)
    if callable(ping):
        return f"{type(executor).__name__} {ping()}"
    raise ConfigError(f"{type(executor).__name__} has no health check")


def _check_ledger(store: Any) -> str:
    conn = getattr(store, "_conn", None)
    if conn is None:
        raise ConfigError("ledger adapter does not expose its schema for inspection")
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    for required in ("ux_triggers_source_external", "ux_triggers_key", "ux_tickets_key"):
        if required not in indexes:
            raise ConfigError(f"ledger is missing unique index {required}")
    version = conn.execute(
        "SELECT value FROM meta WHERE name='schema_version'"
    ).fetchone()
    tickets = getattr(store, "count_tickets", lambda: 0)()
    return f"schema={version[0] if version else '?'} tickets={tickets} indexes={len(indexes)}"


def _run_script(script: Path, root: Path) -> str:
    completed = subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ConfigError(
            (completed.stdout + completed.stderr).strip()[:600] or "layering check failed"
        )
    return (completed.stdout.strip().splitlines() or ["clean"])[-1]


def _main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _main()
