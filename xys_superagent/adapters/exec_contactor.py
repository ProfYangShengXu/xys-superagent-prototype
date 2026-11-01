"""Hand work to the local contactor bridge over JSON-RPC (A2A v0.3 shapes).

The protocol is the Agent2Agent JSON-RPC binding: ``message/send`` starts or
continues a task, ``tasks/get`` reads one.  Task states map onto our report
statuses, not the other way round.

Failure is loud on purpose.  A bridge that cannot be reached raises
:class:`BridgeUnreachable` with the address and a concrete next step; it never
returns "no work" or a fake handle.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

from ..core.ticket import Mandate
from ..core.types import (
    REPORT_BLOCKED,
    REPORT_FAILED,
    REPORT_PROGRESS,
    REPORT_READY,
    REPORT_WAITING,
    Assignment,
    Delivery,
    Message,
    Report,
    WorkRef,
)

# A2A v0.3 application error codes (the ones this adapter reacts to).
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
METHOD_NOT_FOUND = -32601

STATE_MAP = {
    "submitted": REPORT_PROGRESS,
    "working": REPORT_PROGRESS,
    "input-required": REPORT_WAITING,
    "auth-required": REPORT_BLOCKED,
    "completed": REPORT_READY,
    "failed": REPORT_FAILED,
    "canceled": REPORT_BLOCKED,
    "rejected": REPORT_BLOCKED,
    "unknown": REPORT_PROGRESS,
}


class BridgeUnreachable(RuntimeError):
    """No answer from the bridge address."""


class BridgeError(RuntimeError):
    """The bridge answered with a JSON-RPC error."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class TaskNotFound(BridgeError):
    """The task handle is unknown to the bridge."""


class BridgeProtocolError(RuntimeError):
    """The bridge answered something that is not an A2A response."""


def _endpoint_help(endpoint: str) -> str:
    return (
        f"cannot reach the executor bridge at {endpoint}. "
        "Start the bridge, or fix [executor] endpoint in the config. "
        "Note: from inside WSL, 127.0.0.1 is the WSL loopback; a bridge bound "
        "to 127.0.0.1 on Windows is reachable only through the Windows host "
        "address (or WSL mirrored networking), not through 127.0.0.1."
    )


class ContactorExecutor:
    """ExecutorPort backed by the A2A JSON-RPC bridge."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8791/",
        *,
        agent: str = "",
        timeout_s: float = 30.0,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"endpoint must be an http(s) URL, got {endpoint!r}")
        self.endpoint = endpoint
        self.agent = agent
        self.timeout_s = float(timeout_s)
        self._seq = 0

    # -- ExecutorPort -------------------------------------------------------

    def hand_off(self, mandate: Mandate, work: WorkRef) -> Assignment:
        text = self._render(mandate, work)
        params: dict[str, Any] = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                # A stable id makes a retry after a crash safe: the bridge
                # deduplicates on messageId, so the same attempt is not
                # dispatched twice.
                "messageId": work.dispatch_key or uuid.uuid4().hex,
            },
        }
        if self.agent:
            params["metadata"] = {"agent": self.agent}
            params["message"]["metadata"] = {"agent": self.agent}
        result = self._rpc("message/send", params)
        if not isinstance(result, Mapping):
            raise BridgeProtocolError(f"message/send result is not an object: {result!r}")
        handle = result.get("id") or result.get("taskId")
        if not handle:
            raise BridgeProtocolError(
                "message/send did not return a task handle; "
                f"got keys {sorted(result)}"
            )
        return str(handle)

    def deliver(self, assignment: Assignment, message: Message) -> Delivery:
        if message.intent == "probe":
            task = self._get_task(assignment)
            return Delivery(ok=True, note="probe", report=self._to_report(task))
        params: dict[str, Any] = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message.text}],
                "messageId": uuid.uuid4().hex,
                "taskId": assignment,
            },
        }
        if self.agent:
            params["metadata"] = {"agent": self.agent}
        result = self._rpc("message/send", params)
        report = self._to_report(result) if isinstance(result, Mapping) else None
        return Delivery(ok=True, note="sent", report=report)

    def stop(self, assignment: Assignment, reason: str) -> None:
        self._rpc("tasks/cancel", {"id": assignment, "metadata": {"reason": reason}})

    # -- operations used by doctor -----------------------------------------

    def ping(self) -> str:
        """Confirm the endpoint speaks A2A; raise with a clear reason if not."""

        try:
            self._rpc(
                "tasks/get",
                {"id": f"xys_superagent-probe-{uuid.uuid4().hex}", "historyLength": 0},
            )
        except TaskNotFound:
            return f"reachable A2A endpoint at {self.endpoint}"
        except BridgeError as exc:
            if exc.code == METHOD_NOT_FOUND:
                raise BridgeProtocolError(
                    f"{self.endpoint} answered but does not implement tasks/get "
                    f"(JSON-RPC {exc.code}); this is not an A2A v0.3 bridge"
                ) from exc
            raise
        return f"reachable A2A endpoint at {self.endpoint}"

    # -- protocol plumbing ---------------------------------------------------

    def _get_task(self, assignment: Assignment) -> Mapping[str, Any]:
        # historyLength=0 keeps the answer small: the full conversation is not
        # needed to read a state, and it can be thousands of messages long.
        task = self._rpc("tasks/get", {"id": assignment, "historyLength": 0})
        if not isinstance(task, Mapping):
            raise BridgeProtocolError(f"tasks/get result is not an object: {task!r}")
        return task

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        self._seq += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": dict(params)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            self._raise_rpc_error(payload, http_status=exc.code)
            raise
        except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as exc:
            raise BridgeUnreachable(_endpoint_help(self.endpoint)) from exc

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError(
                f"{self.endpoint} returned non-JSON for {method}: {raw[:200]!r}"
            ) from exc
        if not isinstance(envelope, Mapping):
            raise BridgeProtocolError(f"JSON-RPC envelope is not an object: {raw[:200]!r}")
        if "error" in envelope:
            self._raise_rpc_error(envelope)
        if "result" not in envelope:
            raise BridgeProtocolError(
                f"JSON-RPC envelope has neither result nor error: {raw[:200]!r}"
            )
        return envelope["result"]

    def _raise_rpc_error(self, payload: Any, *, http_status: int | None = None) -> None:
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                raise BridgeProtocolError(
                    f"bridge returned HTTP {http_status} with non-JSON body: {payload[:200]!r}"
                ) from None
        else:
            data = payload
        error = data.get("error", data) if isinstance(data, Mapping) else {}
        if not isinstance(error, Mapping):
            error = {}
        code = error.get("code")
        message = str(error.get("message", "")) or f"HTTP {http_status}"
        if code == TASK_NOT_FOUND:
            raise TaskNotFound(f"task not found: {message}", code=TASK_NOT_FOUND)
        if code == TASK_NOT_CANCELABLE:
            raise BridgeError(f"task cannot be cancelled: {message}", code=TASK_NOT_CANCELABLE)
        raise BridgeError(
            f"bridge error {code if code is not None else '(none)'}: {message}",
            code=code if isinstance(code, int) else None,
        )

    def _to_report(self, task: Mapping[str, Any]) -> Report:
        status = task.get("status")
        if not isinstance(status, Mapping):
            raise BridgeProtocolError(f"task has no status object: {task!r}")
        state = str(status.get("state", "unknown"))
        mapped = STATE_MAP.get(state)
        if mapped is None:
            raise BridgeProtocolError(f"unknown task state {state!r} from {self.endpoint}")
        summary = self._message_text(status.get("message")) or str(
            task.get("metadata", {}).get("summary", "")
        )
        return Report(
            status=mapped,
            summary=summary,
            background_running=(state == "working"),
            at=time.time(),
        )

    @staticmethod
    def _message_text(message: Any) -> str:
        if not isinstance(message, Mapping):
            return ""
        parts = message.get("parts")
        if not isinstance(parts, list):
            return ""
        chunks = [
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, Mapping) and part.get("kind", "text") == "text"
        ]
        return "\n".join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _render(mandate: Mandate, work: WorkRef) -> str:
        scopes = ", ".join(mandate.scope_paths) or "(none)"
        actions = ", ".join(sorted(mandate.allowed_actions)) or "(none)"
        return (
            f"workspace: {work.workspace}\n"
            f"branch: {work.branch or '(current)'}\n"
            f"scope: {scopes}\n"
            f"allowed actions: {actions}\n"
            f"max rounds: {mandate.max_rounds}\n"
            f"stop conditions: {', '.join(mandate.stop_conditions) or '(none)'}\n"
            f"work: {work.summary}\n"
            "If the work needs anything outside 'scope', report out_of_scope; "
            "do not widen the scope yourself."
        )
