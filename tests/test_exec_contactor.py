"""The bridge executor against a real local JSON-RPC server (A2A v0.3 shapes)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xys_superagent.adapters.exec_contactor import (
    BridgeProtocolError,
    BridgeUnreachable,
    ContactorExecutor,
    TaskNotFound,
)
from xys_superagent.core.ticket import Mandate
from xys_superagent.core.types import Message, WorkRef


class _State:
    def __init__(self) -> None:
        self.mode = "a2a"
        self.tasks: dict[str, dict] = {}
        self.handles: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def do_POST(self) -> None:  # noqa: N802 - required name
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        state: _State = self.server.state  # type: ignore[attr-defined]
        method = body.get("method")
        params = body.get("params") or {}
        state.calls.append((method, params))

        if state.mode == "http500":
            self._send(500, "oops")
            return
        if state.mode == "empty":
            self._json({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
            return

        if method == "message/send":
            message = params.get("message") or {}
            handle = state.handles.get(str(message.get("messageId")))
            if handle is None:
                handle = f"t{len(state.tasks) + 1}"
                state.handles[str(message.get("messageId"))] = handle
                state.tasks[handle] = {
                    "id": handle,
                    "status": {"state": "working", "message": {"parts": [{"kind": "text", "text": "started"}]}},
                }
            result = state.tasks[handle]
        elif method == "tasks/get":
            handle = str(params.get("id"))
            if handle == "weird":
                result = {"id": "weird", "status": {"state": "bogus"}}
            elif handle in state.tasks:
                result = {
                    "id": handle,
                    "status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "all done"}]}},
                }
            else:
                self._error(body.get("id"), -32001, "task not found")
                return
        elif method == "tasks/cancel":
            result = {"id": params.get("id"), "status": {"state": "canceled"}}
        else:
            self._error(body.get("id"), -32601, "method not found")
            return
        self._json({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    def _send(self, status: int, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, envelope: dict) -> None:
        self._send(200, json.dumps(envelope))

    def _error(self, request_id, code: int, message: str) -> None:
        self._json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )


def _serve(mode: str = "a2a"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = _State()  # type: ignore[attr-defined]
    server.state.mode = mode  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _endpoint(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/"


def _work(dispatch_key: str = "") -> WorkRef:
    return WorkRef(
        workspace="demo",
        branch="fix/parser",
        scope_paths=("src/parser",),
        summary="fix the parser",
        dispatch_key=dispatch_key,
    )


@pytest.fixture
def a2a():
    server = _serve()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_hand_off_sends_agent_scope_and_returns_task_handle(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    handle = executor.hand_off(Mandate(scope_paths=("src/parser",)), _work())

    assert handle == "t1"
    method, params = a2a.state.calls[0]  # type: ignore[attr-defined]
    assert method == "message/send"
    assert params["metadata"]["agent"] == "dsh"
    text = params["message"]["parts"][0]["text"]
    assert "workspace: demo" in text
    assert "scope: src/parser" in text
    assert "out_of_scope" in text


def test_probe_reads_state_without_history(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    handle = executor.hand_off(Mandate(scope_paths=("src/parser",)), _work())
    delivery = executor.deliver(handle, Message(intent="probe"))

    assert delivery.report is not None
    assert delivery.report.status == "ready"
    assert delivery.report.summary == "all done"
    methods = [method for method, _ in a2a.state.calls]  # type: ignore[attr-defined]
    assert "tasks/get" in methods
    get_params = [params for method, params in a2a.state.calls if method == "tasks/get"][0]  # type: ignore[attr-defined]
    assert get_params["historyLength"] == 0


def test_stable_dispatch_key_makes_a_retry_safe(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    first = executor.hand_off(Mandate(scope_paths=("src",)), _work(dispatch_key="t123:r1"))
    second = executor.hand_off(Mandate(scope_paths=("src",)), _work(dispatch_key="t123:r1"))
    assert first == second == "t1"
    third = executor.hand_off(Mandate(scope_paths=("src",)), _work())
    assert third != first


def test_stop_cancels_the_task(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    handle = executor.hand_off(Mandate(scope_paths=("src",)), _work())
    executor.stop(handle, "cancelled by a person")
    methods = [method for method, _ in a2a.state.calls]  # type: ignore[attr-defined]
    assert "tasks/cancel" in methods


def test_ping_confirms_a2a(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    assert "reachable" in executor.ping()


def test_unknown_task_is_a_named_error(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    with pytest.raises(TaskNotFound):
        executor.deliver("does-not-exist", Message(intent="probe"))


def test_unknown_state_is_a_protocol_error(a2a):
    executor = ContactorExecutor(endpoint=_endpoint(a2a), agent="dsh")
    with pytest.raises(BridgeProtocolError, match="unknown task state"):
        executor.deliver("weird", Message(intent="probe"))


def test_unreachable_bridge_is_loud_and_actionable():
    executor = ContactorExecutor(endpoint="http://127.0.0.1:1/", agent="dsh", timeout_s=1.0)
    with pytest.raises(BridgeUnreachable) as caught:
        executor.hand_off(Mandate(scope_paths=("src",)), _work())
    message = str(caught.value)
    assert "cannot reach" in message
    assert "127.0.0.1:1" in message
    assert "WSL" in message


def test_a_server_that_is_not_a2a_is_named():
    server = _serve(mode="empty")
    try:
        executor = ContactorExecutor(endpoint=_endpoint(server), agent="dsh")
        with pytest.raises(BridgeProtocolError, match="task handle"):
            executor.hand_off(Mandate(scope_paths=("src",)), _work())
    finally:
        server.shutdown()
        server.server_close()


def test_http_error_with_plain_text_body_is_a_protocol_error():
    server = _serve(mode="http500")
    try:
        executor = ContactorExecutor(endpoint=_endpoint(server), agent="dsh")
        with pytest.raises(BridgeProtocolError, match="HTTP 500"):
            executor.hand_off(Mandate(scope_paths=("src",)), _work())
    finally:
        server.shutdown()
        server.server_close()
