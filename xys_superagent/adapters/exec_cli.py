"""Hand work to an arbitrary command line.

The second executor implementation exists to prove the port is real: swapping
it for the bridge-backed one changes ``app/build.py`` and nothing in ``core/``
or ``ports/``.  The mandate travels as environment variables, the log stays on
the pipe, and a missing executable raises instead of pretending to start.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from typing import Any, Sequence

from ..core.ticket import Mandate
from ..core.types import (
    REPORT_FAILED,
    REPORT_PROGRESS,
    REPORT_READY,
    Assignment,
    Delivery,
    Message,
    Report,
    WorkRef,
)


class ExecutorUnavailable(RuntimeError):
    """The configured command could not be started."""


class CliExecutor:
    """Run one process per assignment; probes read its exit status."""

    def __init__(
        self,
        command: Sequence[str] | str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        popen: Any = subprocess.Popen,
        log_limit: int = 2000,
    ) -> None:
        if isinstance(command, str):
            argv = shlex.split(command)
        else:
            argv = list(command)
        if not argv:
            raise ValueError("executor.command must not be empty")
        self.command = argv
        self.cwd = cwd
        self._env = dict(env) if env else None
        self._popen = popen
        self.log_limit = log_limit
        self._procs: dict[str, Any] = {}

    # -- ExecutorPort -------------------------------------------------------

    def hand_off(self, mandate: Mandate, work: WorkRef) -> Assignment:
        handle = f"cli-{uuid.uuid4().hex[:12]}"
        environment = dict(os.environ)
        if self._env:
            environment.update(self._env)
        environment["XYS_WORKSPACE"] = work.workspace
        environment["XYS_BRANCH"] = work.branch
        environment["XYS_SCOPE"] = ",".join(mandate.scope_paths)
        environment["XYS_ACTIONS"] = ",".join(sorted(mandate.allowed_actions))
        environment["XYS_MAX_ROUNDS"] = str(mandate.max_rounds)
        environment["XYS_SUMMARY"] = work.summary
        try:
            process = self._popen(
                self.command,
                cwd=self.cwd,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ExecutorUnavailable(
                f"executor command not found: {self.command[0]!r}; "
                "fix [executor] command in the config"
            ) from exc
        except OSError as exc:
            raise ExecutorUnavailable(f"cannot start {self.command!r}: {exc}") from exc
        self._procs[handle] = process
        return handle

    def deliver(self, assignment: Assignment, message: Message) -> Delivery:
        process = self._procs.get(assignment)
        if process is None:
            return Delivery(False, f"unknown assignment {assignment!r}")
        if message.intent == "probe":
            return self._probe(assignment, process)
        if process.poll() is not None:
            return Delivery(False, f"process already exited with {process.returncode}")
        try:
            assert process.stdin is not None
            process.stdin.write(message.text + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            return Delivery(False, f"stdin closed: {exc}")
        return Delivery(True, "written to stdin")

    def stop(self, assignment: Assignment, reason: str) -> None:
        process = self._procs.pop(assignment, None)
        if process is None:
            return
        if process.poll() is not None:
            self._close(process)
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except (OSError, ProcessLookupError):
            pass
        self._close(process)

    def close(self) -> None:
        for handle in list(self._procs):
            self.stop(handle, "executor closed")

    def __enter__(self) -> "CliExecutor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _probe(self, handle: str, process: Any) -> Delivery:
        code = process.poll()
        if code is None:
            return Delivery(
                True,
                "running",
                Report(status=REPORT_PROGRESS, summary="", at=time.time()),
            )
        out = self._read(process.stdout)
        err = self._read(process.stderr)
        self._close(process)
        self._procs.pop(handle, None)
        status = REPORT_READY if code == 0 else REPORT_FAILED
        summary = (out or err).strip()[-self.log_limit :]
        return Delivery(
            True,
            f"exit {code}",
            Report(status=status, summary=summary, at=time.time()),
        )

    @staticmethod
    def _read(stream: Any) -> str:
        if stream is None:
            return ""
        try:
            return stream.read() or ""
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _close(process: Any) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
