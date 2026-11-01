"""C1: swapping the executor does not touch core/ or ports/."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from xys_superagent.app.build import build_app

from conftest import ROOT, write_events


def fingerprint(root: Path = ROOT) -> str:
    """Hash of every core/ and ports/ file, path included."""

    digest = hashlib.sha256()
    for layer in ("core", "ports"):
        for path in sorted((root / "xys_superagent" / layer).rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _config(tmp_path: Path, name: str, executor: dict) -> dict:
    events = write_events(tmp_path / f"{name}.jsonl", [_event()])
    return {
        "app": {"database": str(tmp_path / f"{name}.db")},
        "policy": {"allowed_people": ["alice"]},
        "sources": {
            "local": {"kind": "file", "path": str(events), "workspace": "demo"}
        },
        "executor": executor,
        "panel": {"kind": "text"},
    }


def _event() -> dict:
    return {
        "external_id": "e1",
        "actor": "alice",
        "actor_kind": "person",
        "text": "please fix the parser",
        "workspace": "demo",
        "scope_paths": ["src/parser"],
        "mentions_agent": True,
    }


def test_c1_swapping_executor_keeps_core_and_ports(tmp_path):
    before = fingerprint()
    command_line = build_app(
        _config(tmp_path, "cli", {"kind": "cli", "command": ["sh", "-c", "echo hi"]})
    )
    bridge = build_app(
        _config(
            tmp_path,
            "bridge",
            {
                "kind": "contactor",
                "endpoint": "http://127.0.0.1:1/",
                "agent": "dsh",
            },
        )
    )
    try:
        assert type(command_line.executor) is not type(bridge.executor)
        for executor in (command_line.executor, bridge.executor):
            for method in ("hand_off", "deliver", "stop"):
                assert callable(getattr(executor, method))
    finally:
        command_line.close()
        bridge.close()

    assert fingerprint() == before, "core/ or ports/ changed while swapping an adapter"


def test_c1_layering_script_agrees_on_the_real_tree():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_layering.py")],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "no cycles, no reverse dependencies" in completed.stdout


def test_c1_negative_reverse_import_is_caught(tmp_path):
    """The counter-example: an adapters import inside core must fail the check."""

    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "xys_superagent", tree / "xys_superagent")
    (tree / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check_layering.py", tree / "scripts" / "check_layering.py")

    stage = tree / "xys_superagent" / "core" / "stage.py"
    stage.write_text(
        stage.read_text(encoding="utf-8")
        + "\nfrom xys_superagent.adapters import exec_cli  # injected by the C1 counter-example\n",
        encoding="utf-8",
    )

    assert fingerprint(tree) != fingerprint(ROOT)

    completed = subprocess.run(
        [sys.executable, str(tree / "scripts" / "check_layering.py"), "--root", str(tree)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "reverse dependency" in completed.stdout
    assert "core -> adapters" in completed.stdout
