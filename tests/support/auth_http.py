from __future__ import annotations

import http.client
import json
import multiprocessing
import queue
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock



from gp_control_plane.backups import (  # noqa: E402
    clean_install_handoff_path,
    clean_install_vault_dir,
    create_clean_install_vault,
    restore_clean_install_vault,
)
from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.storage import StorageUnavailableError, append_run, db_path
from gp_control_plane.strategy_finder import _LiveStdoutRecorder, upsert_candidate_event_conn as storage_upsert_candidate_event_conn
from gp_control_plane.web.api_server import serve
from tests.support.web_process import serve_web_proxy, start_web_process


_PROCESS_TIMEOUT_SECONDS = 15


def _chained_storage_unavailable_517() -> StorageUnavailableError:
    cause = sqlite3.OperationalError("database is locked: /private/path SELECT secret")
    cause.sqlite_errorcode = 517
    cause.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"
    error = StorageUnavailableError("storage is temporarily unavailable")
    error.__cause__ = cause
    return error


def _serve_core_in_process(
    state_dir_raw: str,
    port: int,
    ready: Any,
    stop: Any,
    errors: Any,
) -> None:
    """Serve a core role in a spawned interpreter, stopping through an Event."""
    import gp_control_plane.web.api_server as api_server

    config = AppConfig(output=OutputConfig(state_dir=Path(state_dir_raw)))
    try:
        from gp_control_plane.web.core_runtime import create_core_runtime

        runtime = create_core_runtime(config, "127.0.0.1", port, ui_enabled=False)
        thread = threading.Thread(target=runtime.serve_forever, name="auth-core-cheroot", daemon=True)
        thread.start()
        deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Core Cheroot child did not become ready")
                time.sleep(0.01)
        ready.set()
        if not stop.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("parent did not stop Core Cheroot child")
        runtime.stop()
        thread.join(timeout=5)
        if thread.is_alive():
            raise TimeoutError("Core Cheroot child did not stop")
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        errors.put(repr(error))
        ready.set()


def _hold_sqlite_immediate_transaction_in_process(
    state_dir_raw: str,
    entered: Any,
    release: Any,
    errors: Any,
) -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path(Path(state_dir_raw)), timeout=0)
        conn.execute("BEGIN IMMEDIATE")
        entered.set()
        if not release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
            raise TimeoutError("parent did not release the SQLite write lock")
        conn.rollback()
    except BaseException as error:  # noqa: BLE001 - report child failures to the parent test
        errors.put(repr(error))
    finally:
        if conn is not None:
            conn.close()


def _join_process(test: unittest.TestCase, process: multiprocessing.Process) -> None:
    process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        test.fail(f"child process {process.name} did not stop")
    test.assertEqual(process.exitcode, 0, f"child process {process.name} exited unexpectedly")




class _ManagedServer:
    def __init__(self, port: int, server: Any, thread: threading.Thread) -> None:
        self.port = port
        self._server = server
        self._thread = thread

    def close(self) -> None:
        if getattr(self,"_closed",False):
            return
        self._closed=True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError(f"server thread did not stop on port {self.port}")


def _start_managed_server(function: Any, config: AppConfig, **kwargs: Any) -> _ManagedServer:
    port = _free_port()
    module = sys.modules[function.__module__]
    if function is serve_web_proxy:
        return start_web_process(port, kwargs["core_url"])

    if function.__name__ in {"serve", "serve_core"} and function.__module__ in {
        "gp_control_plane.web.api_server",
        "gp_control_plane.web.core_server",
        "gp_control_plane.http.api_server",
        "gp_control_plane.http.core_server",
    }:
        from gp_control_plane.web.core_runtime import create_core_runtime

        runtime = create_core_runtime(
            config,
            "127.0.0.1",
            port,
            ui_enabled=bool(kwargs.get("ui_enabled", False)),
        )
        startup_errors: list[BaseException] = []

        def run_cheroot() -> None:
            try:
                runtime.serve_forever()
            except BaseException as error:
                startup_errors.append(error)

        thread = threading.Thread(target=run_cheroot, daemon=True)
        thread.start()
        managed = _ManagedServer(port, runtime, thread)
        try:
            _wait_for_server(port)
        except BaseException:
            managed.close()
            raise
        if startup_errors:
            managed.close()
            raise AssertionError(f"Cheroot Core on port {port} failed during startup") from startup_errors[0]
        return managed

    server_type = getattr(module, "ThreadingHTTPServer")
    server_created = threading.Event()
    server_holder: dict[str, Any] = {}
    startup_errors: list[BaseException] = []

    class CapturingThreadingHTTPServer(server_type):
        def __init__(self, *args: Any, **server_kwargs: Any) -> None:
            super().__init__(*args, **server_kwargs)
            server_holder["server"] = self
            server_created.set()

    def run() -> None:
        try:
            function(config, "127.0.0.1", port, **kwargs)
        except BaseException as error:
            startup_errors.append(error)
            server_created.set()

    thread = threading.Thread(target=run, daemon=True)
    with mock.patch.object(module, "ThreadingHTTPServer", CapturingThreadingHTTPServer):
        thread.start()
        if not server_created.wait(timeout=5):
            raise AssertionError(f"server on port {port} did not construct")

    if startup_errors:
        raise AssertionError(f"server on port {port} failed during startup") from startup_errors[0]
    server = server_holder.get("server")
    if server is None:
        raise AssertionError(f"server on port {port} was not captured")
    managed = _ManagedServer(port, server, thread)
    try:
        _wait_for_server(port)
    except BaseException:
        managed.close()
        raise
    return managed


def _close_sse(
    connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None
) -> None:
    try:
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()



_case_owners: list[Any] = []

def _start_server(function: Any, config: AppConfig, **kwargs: Any) -> int:
    server = _start_managed_server(function, config, **kwargs)
    _case_owners.append(server)
    return server.port


def _request(
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    finally:
        connection.close()


def _open_sse(port: int, headers: dict[str, str], *, path: str = "/api/web/events/stream") -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers=headers)
    return connection, connection.getresponse()


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            status, _headers, _body = _request(port, "/api/health")
        except OSError:
            time.sleep(0.02)
            continue
        if status == 200:
            return
        time.sleep(0.02)
    raise AssertionError(f"server on port {port} did not become ready")


def _wait_for_stopped_run(
    test: unittest.TestCase, port: int, headers: dict[str, str], run_id: str
) -> None:
    """Wait for the real JobRunner cancellation path to reach idle/stopped."""
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, _response_headers, body = _request(port, "/api/core/runs/history", headers=headers)
        if status == 200:
            history = json.loads(body).get("runs", [])
            stopped = next(
                (item for item in history if item.get("run_id") == run_id and item.get("status") == "stopped"),
                None,
            )
            status_code, _status_headers, status_body = _request(port, "/api/core/status", headers=headers)
            if stopped is not None and status_code == 200 and json.loads(status_body).get("state") == "idle":
                return
        time.sleep(0.02)
    test.fail(f"run {run_id} did not reach stopped/idle through the API")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")




class OwnedHttpCase(unittest.TestCase):
    def tearDown(self):
        failures=[]
        while _case_owners:
            try: _case_owners.pop().close()
            except BaseException as error: failures.append(error)
        if failures: raise ExceptionGroup("HTTP owner cleanup failures",failures)
