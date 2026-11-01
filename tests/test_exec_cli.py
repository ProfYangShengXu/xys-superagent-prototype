"""The command-line executor: a real second implementation of ExecutorPort."""

from __future__ import annotations

import time

import pytest

from xys_superagent.adapters.exec_cli import CliExecutor, ExecutorUnavailable
from xys_superagent.core.ticket import Mandate
from xys_superagent.core.types import REPORT_READY, Message, WorkRef


def _work() -> WorkRef:
    return WorkRef(workspace="demo", scope_paths=("src",), summary="echo")


def _wait_for_exit(executor: CliExecutor, handle: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        delivery = executor.deliver(handle, Message(intent="probe"))
        if delivery.report is not None and delivery.report.status != "progress":
            return delivery
        time.sleep(0.02)
    raise AssertionError("process did not finish in time")


def test_cli_runs_a_command_and_reports_ready():
    executor = CliExecutor(["sh", "-c", "echo hello"])
    try:
        handle = executor.hand_off(Mandate(scope_paths=("src",)), _work())
        delivery = _wait_for_exit(executor, handle)
        assert delivery.report is not None
        assert delivery.report.status == REPORT_READY
        assert "hello" in delivery.report.summary
    finally:
        executor.close()


def test_cli_reports_failure_exit_code():
    executor = CliExecutor(["sh", "-c", "echo boom >&2; exit 3"])
    try:
        handle = executor.hand_off(Mandate(scope_paths=("src",)), _work())
        delivery = _wait_for_exit(executor, handle)
        assert delivery.report is not None
        assert delivery.report.status == "failed"
        assert "boom" in delivery.report.summary
    finally:
        executor.close()


def test_cli_sends_messages_to_a_running_process():
    executor = CliExecutor(["sh", "-c", "cat"])
    try:
        handle = executor.hand_off(Mandate(scope_paths=("src",)), _work())
        running = executor.deliver(handle, Message(intent="probe"))
        assert running.report is None or running.report.status == "progress"
        sent = executor.deliver(handle, Message(text="do the thing"))
        assert sent.ok
    finally:
        executor.close()


def test_cli_stop_ends_the_process():
    executor = CliExecutor(["sh", "-c", "cat"])
    handle = executor.hand_off(Mandate(scope_paths=("src",)), _work())
    executor.stop(handle, "test done")
    probe = executor.deliver(handle, Message(intent="probe"))
    assert probe.ok is False  # unknown handle after stop


def test_cli_missing_command_is_named():
    executor = CliExecutor(["definitely-not-a-real-binary-xyz"])
    with pytest.raises(ExecutorUnavailable, match="not found"):
        executor.hand_off(Mandate(scope_paths=("src",)), _work())


def test_cli_passes_the_mandate_through_the_environment(tmp_path):
    out = tmp_path / "env.txt"
    executor = CliExecutor(
        ["sh", "-c", f'printf "%s|%s" "$XYS_WORKSPACE" "$XYS_SCOPE" > {out}']
    )
    try:
        handle = executor.hand_off(Mandate(scope_paths=("src", "docs")), _work())
        _wait_for_exit(executor, handle)
        assert out.read_text(encoding="utf-8") == "demo|src,docs"
    finally:
        executor.close()
