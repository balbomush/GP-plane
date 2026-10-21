from __future__ import annotations

import http.client
import json
import os
import errno
import socket
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.state import read_state, update_state
from gp_control_plane.web.api_server import serve
from gp_control_plane.web import api_server



class TestServerLifecycleTests(unittest.TestCase):
    def test_primary_body_failure_is_preserved_when_server_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            server = _TestServer(AppConfig(output=OutputConfig(state_dir=Path(raw) / "state")))
            primary = AssertionError("primary browser assertion")
            with patch.object(server, "_stop", side_effect=AssertionError("forced server cleanup failure")):
                self.assertFalse(server.__exit__(AssertionError, primary, None))
            self.assertIn("primary browser assertion", str(primary))
            self.assertIn("forced server cleanup failure", "\n".join(getattr(primary, "__notes__", ())))

    def test_active_root_managed_startup_keeps_state_when_generic_recovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            run_id = "interrupted-root-run"
            update_state(
                config.output.state_dir,
                lambda state: {
                    **state,
                    "current_run_id": run_id,
                    "current_run_name": "zapret-multi-domain-discovery",
                    "current_run_status": "running",
                },
            )

            with patch.object(api_server, "recover_registered_process_runs", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "managed runtime recovery could not be verified"):
                    api_server._recover_runtime_before_serve(config)

            blocked = read_state(config.output.state_dir)
            self.assertEqual(blocked["current_run_id"], run_id)
            self.assertEqual(blocked["current_run_status"], "running")

            with patch.object(api_server, "recover_registered_process_runs", return_value=True):
                api_server._recover_runtime_before_serve(config)

            released = read_state(config.output.state_dir)
            self.assertIsNone(released["current_run_id"])
            self.assertIsNone(released["current_run_status"])

    def test_quarantined_startup_requires_matching_root_recovery_before_state_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            run_id = "quarantined-run"
            update_state(
                config.output.state_dir,
                lambda state: {
                    **state,
                    "current_run_id": run_id,
                    "current_run_name": "zapret-multi-domain-discovery",
                    "current_run_status": "quarantined",
                },
            )
            with patch.object(api_server, "recover_quarantined_process_run", side_effect=RuntimeError("root artifacts missing")):
                with self.assertRaisesRegex(RuntimeError, "root artifacts missing"):
                    api_server._recover_runtime_before_serve(config)
            blocked = read_state(config.output.state_dir)
            self.assertEqual(blocked["current_run_id"], run_id)
            self.assertEqual(blocked["current_run_status"], "quarantined")

            with patch.object(api_server, "recover_quarantined_process_run") as recovered:
                api_server._recover_runtime_before_serve(config)

            recovered.assert_called_once_with(run_id)
            released = read_state(config.output.state_dir)
            self.assertIsNone(released["current_run_id"])
            self.assertIsNone(released["current_run_status"])

    def test_current_runtime_releases_its_listener_during_context_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            server = _TestServer(
                AppConfig(output=OutputConfig(state_dir=Path(raw) / "state")),
            )
            with server:
                self.assertIsNotNone(server._runtime)
                self.assertIsNotNone(server._thread)
                self.assertTrue(server._thread.is_alive())
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", server.port))


class _TestServer:
    def __init__(
        self,
        config: AppConfig,
        *,
        startup_timeout: float = 5,
        server_type: type[Any] | None = None,
        startup_timeout_gate: threading.Event | None = None,
    ):
        self._config = config
        self._startup_timeout = startup_timeout
        self._server_type = server_type
        self._startup_timeout_gate = startup_timeout_gate
        self.port = _free_port()
        self._server: Any | None = None
        self._runtime: Any | None = None
        self._thread: threading.Thread | None = None
        self._startup_lock = threading.Lock()
        self._startup_cancelled = threading.Event()
        self._request_cancelled = threading.Event()
        self._serving = threading.Event()
        self._sleep_patch: Any | None = None

    def _interruptible_server_sleep(self, seconds: float) -> None:
        if self._request_cancelled.wait(timeout=seconds):
            raise _ServerRequestCancelled()

    def __enter__(self) -> "_TestServer":
        # A6 replaced the product BaseHTTP listener.  Browser UI checks still
        # need a real listener, but must not retain a second test-only product
        # stack or patch the old handler's private lifecycle.
        if self._server_type is None and self._startup_timeout_gate is None:
            from gp_control_plane.web.core_runtime import create_core_runtime

            self._runtime = create_core_runtime(self._config, "127.0.0.1", self.port, ui_enabled=False)
            self._thread = threading.Thread(target=self._runtime.serve_forever, daemon=True)
            self._thread.start()
            try:
                _wait_for_http(f"http://127.0.0.1:{self.port}/api/health", timeout=self._startup_timeout)
            except BaseException:
                self._stop()
                raise
            return self

        ready = threading.Event()
        original_server = self._server_type or api_server.ThreadingHTTPServer
        self._startup_cancelled.clear()
        self._request_cancelled.clear()
        self._serving.clear()

        owner = self

        class CapturingServer(original_server):
            # Browser bootstrap opens long-lived SSE/HTTP requests. Track their
            # sockets so teardown closes the clients before joining handlers and
            # TemporaryDirectory removes the state database on Windows.
            daemon_threads = False
            block_on_close = False

            def __init__(self, *args: Any, **kwargs: Any):
                self._active_requests: set[socket.socket] = set()
                self._active_requests_lock = threading.Lock()
                self._request_workers: set[threading.Thread] = set()
                self._request_workers_lock = threading.Lock()
                self._worker_requests: dict[threading.Thread, socket.socket] = {}
                self._request_details: dict[socket.socket, str] = {}
                super().__init__(*args, **kwargs)
                with owner._startup_lock:
                    owner._server = self
                    ready.set()
                    if owner._startup_cancelled.is_set():
                        self.server_close()
                        raise _ServerStartupCancelled()

            def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
                worker = threading.current_thread()
                with self._active_requests_lock:
                    self._active_requests.add(request)
                with self._request_workers_lock:
                    self._request_workers.add(worker)
                    self._worker_requests[worker] = request
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with self._active_requests_lock:
                        self._active_requests.discard(request)
                        self._request_details.pop(request, None)
                    with self._request_workers_lock:
                        self._request_workers.discard(worker)
                        self._worker_requests.pop(worker, None)

            def finish_request(self, request: socket.socket, client_address: Any) -> None:
                handler_class = self.RequestHandlerClass
                server = self

                class TrackingHandler(handler_class):
                    def do_GET(self) -> None:  # noqa: N802
                        with server._active_requests_lock:
                            server._request_details[self.request] = f"{client_address[0]}:{client_address[1]} {self.path}"
                        try:
                            super().do_GET()
                        finally:
                            if self.path == "/api/web/events/stream":
                                self.close_connection = True

                TrackingHandler(request, client_address, self)

            def close_active_requests(self) -> list[str]:
                errors: list[str] = []
                with self._active_requests_lock:
                    requests = tuple(self._active_requests)
                for request in requests:
                    try:
                        request.shutdown(socket.SHUT_RDWR)
                    except OSError as error:
                        if not _expected_connection_close(error):
                            errors.append(f"request shutdown: {error}")
                    try:
                        request.close()
                    except OSError as error:
                        if not _expected_connection_close(error):
                            errors.append(f"request close: {error}")
                return errors

            def wake_request_workers(self) -> None:
                owner._request_cancelled.set()

            def join_request_workers(self, timeout: float = 5) -> None:
                with self._request_workers_lock:
                    workers = tuple(self._request_workers)
                for worker in workers:
                    worker.join(timeout=timeout)
                with self._request_workers_lock:
                    active = [
                        f"{worker.name} ({self._request_details.get(self._worker_requests.get(worker), 'path unavailable')})"
                        for worker in workers
                        if worker.is_alive()
                    ]
                if active:
                    raise AssertionError(f"test server request workers did not stop cleanly: {active}")

            def handle_error(self, request: socket.socket, client_address: Any) -> None:
                # Closing a keep-alive socket deliberately unblocks its handler
                # during teardown; it is not a product-server failure.
                active_error = sys.exc_info()[1]
                if owner._startup_cancelled.is_set() and isinstance(active_error, OSError) and _expected_connection_close(active_error):
                    return
                super().handle_error(request, client_address)

            def serve_forever(self, *args: Any, **kwargs: Any) -> None:
                with owner._startup_lock:
                    if owner._startup_cancelled.is_set():
                        return
                    owner._serving.set()
                super().serve_forever(*args, **kwargs)

        def run_server() -> None:
            try:
                serve(self._config, "127.0.0.1", self.port)
            except _ServerStartupCancelled:
                return

        with patch.object(api_server, "ThreadingHTTPServer", CapturingServer):
            try:
                self._sleep_patch = patch.object(api_server.time, "sleep", self._interruptible_server_sleep)
                self._sleep_patch.start()
                self._thread = threading.Thread(target=run_server, daemon=True)
                self._thread.start()
                if self._startup_timeout_gate is not None and not self._startup_timeout_gate.wait(timeout=5):
                    raise AssertionError("test server did not reach the startup timeout gate")
                if not ready.wait(timeout=self._startup_timeout):
                    raise AssertionError("test server did not bind its HTTP listener")
                _wait_for_http(f"http://127.0.0.1:{self.port}/api/health")
            except BaseException:
                self._stop()
                raise
        return self

    def __exit__(self, _type: object, value: BaseException | None, _traceback: object) -> bool:
        try:
            self._stop()
        except BaseException as error:
            if value is not None:
                _attach_cleanup_note(value, str(error))
                return False
            raise
        return False

    def _stop(self) -> None:
        errors: list[str] = []
        if self._runtime is not None:
            try:
                self._runtime.stop()
            except BaseException as error:
                errors.append(f"runtime stop: {error}")
            if self._thread is not None:
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    errors.append("test runtime did not stop cleanly")
            if errors:
                raise AssertionError("BGT-001 test server cleanup failed: " + "; ".join(errors))
            return

        with self._startup_lock:
            self._startup_cancelled.set()
            server = self._server
            serving = self._serving.is_set()

        if server is not None and serving:
            try:
                server.shutdown()
            except BaseException as error:
                errors.append(f"server shutdown: {error}")
        if server is not None:
            try:
                errors.extend(server.close_active_requests())
            except BaseException as error:
                errors.append(f"request close: {error}")
            try:
                server.wake_request_workers()
            except BaseException as error:
                errors.append(f"request worker wakeup: {error}")
            try:
                server.server_close()
            except BaseException as error:
                errors.append(f"server close: {error}")
        try:
            if server is not None:
                try:
                    server.join_request_workers()
                except BaseException as error:
                    errors.append(f"request worker join: {error}")
            if self._thread is not None:
                try:
                    self._thread.join(timeout=5)
                except BaseException as error:
                    errors.append(f"server thread join: {error}")
                if self._thread.is_alive():
                    errors.append("test server did not stop cleanly")
        finally:
            if self._sleep_patch is not None:
                try:
                    self._sleep_patch.stop()
                except BaseException as error:
                    errors.append(f"sleep patch stop: {error}")
                finally:
                    self._sleep_patch = None
        if errors:
            raise AssertionError("BGT-001 test server cleanup failed: " + "; ".join(errors))


class _ServerStartupCancelled(Exception):
    pass


class _ServerRequestCancelled(Exception):
    pass


def _expected_connection_close(error: OSError) -> bool:
    return error.errno in {errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTCONN} or getattr(error, "winerror", None) in {10053, 10054, 10058}


def _attach_cleanup_note(error: BaseException, cleanup: str) -> None:
    if hasattr(error, "add_note"):
        error.add_note("BGT-001 cleanup: " + cleanup)


def _create_backup(port: int) -> str:
    status, body = _http_request(
        port,
        "POST",
        "/api/auth/login",
        {"Content-Type": "application/json"},
        json.dumps({"username": "admin", "password": "admin"}).encode("utf-8"),
    )
    if status != 200:
        raise AssertionError(f"test setup login failed with HTTP {status}: {body!r}")
    token = json.loads(body)["access_token"]
    status, body = _http_request(
        port,
        "POST",
        "/api/core/backups/create",
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        b"{}",
    )
    if status != 201:
        raise AssertionError(f"test setup backup creation failed with HTTP {status}: {body!r}")
    return str(json.loads(body)["snapshot_id"])


def _http_request(port: int, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except (OSError, ValueError) as error:
            last_error = error
            time.sleep(0.02)
    raise AssertionError(f"server did not become ready: {last_error}")


if __name__ == '__main__':
    unittest.main()
