"""C8: the layering script reports cycles and reverse dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from conftest import ROOT


def _copy_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "xys_superagent", tree / "xys_superagent")
    (tree / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check_layering.py", tree / "scripts" / "check_layering.py")
    return tree


def _run(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "check_layering.py"), "--root", str(tree)],
        capture_output=True,
        text=True,
    )


def test_c8_real_tree_is_clean():
    completed = _run(ROOT)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "scanned" in completed.stdout
    assert "no cycles, no reverse dependencies" in completed.stdout


def test_c8_negative_reverse_dependency(tmp_path):
    tree = _copy_tree(tmp_path)
    target = tree / "xys_superagent" / "core" / "stage.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nfrom xys_superagent.adapters import exec_cli  # injected\n",
        encoding="utf-8",
    )
    completed = _run(tree)
    assert completed.returncode != 0
    assert "reverse dependency" in completed.stdout
    assert "core -> adapters" in completed.stdout


def test_c8_negative_sibling_adapters(tmp_path):
    tree = _copy_tree(tmp_path)
    target = tree / "xys_superagent" / "adapters" / "exec_cli.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\nfrom .store_sqlite import SqliteStore  # injected: adapters must not import each other\n",
        encoding="utf-8",
    )
    completed = _run(tree)
    assert completed.returncode != 0
    assert "must not import each other" in completed.stdout


def test_c8_negative_cycle(tmp_path):
    tree = _copy_tree(tmp_path)
    adapters = tree / "xys_superagent" / "adapters"
    (adapters / "cycle_a.py").write_text(
        "from .cycle_b import helper\n\ndef one():\n    return helper()\n", encoding="utf-8"
    )
    (adapters / "cycle_b.py").write_text(
        "from .cycle_a import one\n\ndef helper():\n    return one()\n", encoding="utf-8"
    )
    completed = _run(tree)
    assert completed.returncode != 0
    assert "cycle" in completed.stdout.lower()


def test_c8_negative_third_party_in_core(tmp_path):
    tree = _copy_tree(tmp_path)
    target = tree / "xys_superagent" / "core" / "stage.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nimport yaml  # injected\n",
        encoding="utf-8",
    )
    completed = _run(tree)
    assert completed.returncode != 0
    assert "third-party import" in completed.stdout
