"""Ports must stay free of transport vocabulary."""

from __future__ import annotations

import inspect
from pathlib import Path

from xys_superagent.ports.executor import ExecutorPort
from xys_superagent.ports.panel import PanelPort
from xys_superagent.ports.source import SourcePort
from xys_superagent.ports.store import StorePort

from conftest import ROOT

FORBIDDEN = ("github", "sqlite", "html", "task_id", "prompt", "jsonl", "http")


def hits(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in FORBIDDEN if word in lowered]


def test_ports_have_no_transport_words():
    ports = ROOT / "xys_superagent" / "ports"
    for path in sorted(ports.glob("*.py")):
        found = hits(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} leaks {found}"


def test_port_signatures():
    assert list(inspect.signature(SourcePort.next_batch).parameters) == ["self", "cursor"]
    assert list(inspect.signature(SourcePort.acknowledge).parameters) == ["self", "cursor"]
    assert list(inspect.signature(ExecutorPort.hand_off).parameters) == [
        "self",
        "mandate",
        "work",
    ]
    assert list(inspect.signature(ExecutorPort.deliver).parameters) == [
        "self",
        "assignment",
        "message",
    ]
    assert list(inspect.signature(StorePort.append).parameters) == ["self", "record"]
    assert list(inspect.signature(StorePort.since).parameters) == ["self", "cursor"]
    assert list(inspect.signature(StorePort.find).parameters) == ["self", "key"]
    assert list(inspect.signature(PanelPort.publish).parameters) == ["self", "view"]


def test_vocabulary_check_has_teeth():
    assert hits("this adapter reads GitHub notifications") == ["github"]
    assert hits("SELECT * FROM sqlite_master") == ["sqlite"]
    assert hits("return the task_id") == ["task_id"]
    assert hits("no forbidden words here") == []
