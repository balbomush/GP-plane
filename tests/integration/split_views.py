from __future__ import annotations

import http.client
import json
import multiprocessing
import queue
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




from gp_control_plane.config import AppConfig, OutputConfig  # noqa: E402
from tests.browser.runner import PlaywrightPage  # noqa: E402


_PROCESS_TIMEOUT_SECONDS = 12


def _serve_core_process(
    state_dir_raw: str,
    port: int,
    ready: Any,
    stop: Any,
    errors: Any,
    force_view_503: Any,
    stream_entered: Any,
    stream_release: Any,
) -> None:
    from gp_control_plane.storage import StorageUnavailableError
    from gp_control_plane.web import api_server

    config = AppConfig(output=OutputConfig(state_dir=Path(state_dir_raw)))
    original_get_payload = api_server.web_views.get_payload
    original_event_payloads = api_server._event_payloads

    def get_payload(config: AppConfig, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        if force_view_503.is_set():
            raise StorageUnavailableError("storage is temporarily unavailable")
        return original_get_payload(config, path, query)

    def event_payloads(config: AppConfig) -> dict[str, dict[str, Any]]:
        if stream_entered is not None and not stream_entered.is_set():
            stream_entered.set()
            if stream_release is not None and not stream_release.wait(timeout=_PROCESS_TIMEOUT_SECONDS):
                raise TimeoutError("parent did not release the Core SSE frame gate")
        return original_event_payloads(config)

    try:
        from gp_control_plane.web.core_runtime import create_core_runtime

        with (
            mock.patch.object(api_server.web_views, "get_payload", side_effect=get_payload),
            mock.patch.object(api_server, "_event_payloads", side_effect=event_payloads),
        ):
            runtime = create_core_runtime(config, "127.0.0.1", port, ui_enabled=False)
            thread = threading.Thread(target=runtime.serve_forever, name="a4-core-cheroot", daemon=True)
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
            if not stop.wait(timeout=120):
                raise TimeoutError("parent did not stop Core Cheroot child")
            runtime.stop()
            thread.join(timeout=5)
            if thread.is_alive():
                raise TimeoutError("Core Cheroot child did not stop")
    except BaseException as error:  # noqa: BLE001 - child errors are asserted by the parent
        errors.put(repr(error))
        ready.set()


def _serve_web_process(state_dir_raw,port,core_url,ready,stop,errors):
    # State path is intentionally unused in the independent Web process.
    from tests.support.web_process import start_web_process
    owner=None
    try:
        owner=start_web_process(port,core_url,deny_sqlite=True)
        ready.set()
        if not stop.wait(timeout=120):
            raise TimeoutError('parent did not stop installed Web owner')
    except BaseException as error:
        errors.put(repr(error));ready.set()
    finally:
        if owner is not None:
            try:owner.close()
            except BaseException as error:errors.put(repr(error))


class _SplitRuntime:
    def __init__(self, state_dir: Path, *, sse_gate: bool = False) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._errors = self._context.Queue()
        self._core_ready = self._context.Event()
        self._web_ready = self._context.Event()
        self._core_stop = self._context.Event()
        self._web_stop = self._context.Event()
        self.force_view_503 = self._context.Event()
        self.stream_entered = self._context.Event() if sse_gate else None
        self.stream_release = self._context.Event() if sse_gate else None
        self.core_port = _free_port()
        self.web_port = _free_port()
        self._core = self._context.Process(
            target=_serve_core_process,
            args=(
                str(state_dir),
                self.core_port,
                self._core_ready,
                self._core_stop,
                self._errors,
                self.force_view_503,
                self.stream_entered,
                self.stream_release,
            ),
            name="a4-core",
        )
        self._web = self._context.Process(
            target=_serve_web_process,
            args=(
                str(state_dir),
                self.web_port,
                f"http://127.0.0.1:{self.core_port}",
                self._web_ready,
                self._web_stop,
                self._errors,
            ),
            name="a4-web-no-db",
        )

    def start(self, test: unittest.TestCase) -> None:
        self._core.start()
        test.assertTrue(self._core_ready.wait(timeout=_PROCESS_TIMEOUT_SECONDS), "Core did not become ready")
        self._raise_child_error(test)
        _wait_for_health(test, self.core_port)
        self._web.start()
        test.assertTrue(self._web_ready.wait(timeout=_PROCESS_TIMEOUT_SECONDS), "Web did not become ready")
        self._raise_child_error(test)
        _wait_for_health(test, self.web_port)

    def close(self, test: unittest.TestCase) -> None:
        self._web_stop.set()
        self._core_stop.set()
        for process in (self._web, self._core):
            process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                test.fail(f"child process {process.name} did not stop")
            test.assertEqual(process.exitcode, 0, f"child process {process.name} exited unexpectedly")
        self._raise_child_error(test)

    def _raise_child_error(self, test: unittest.TestCase) -> None:
        try:
            error = self._errors.get_nowait()
        except queue.Empty:
            return
        test.fail(f"split child failed: {error}")


class A4SplitRuntimeTests(unittest.TestCase):
    def test_core_owned_web_views_are_served_over_real_http_without_web_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _SplitRuntime(Path(raw) / "state")
            runtime.start(self)
            try:
                token = _login(self, runtime.web_port)
                bearer = {"Authorization": f"Bearer {token}"}
                for path in (
                    "/api/web/status",
                    "/api/web/run-preferences",
                    "/api/web/runs/history-page",
                    "/api/web/candidate-domain-index-page",
                    "/api/web/strategy-candidates-page",
                    "/api/web/presets",
                    "/api/web/presets/domains",
                    "/api/web/events",
                ):
                    status, _headers, body = _request(runtime.web_port, path, headers=bearer)
                    self.assertEqual(status, 200, f"{path}: {body!r}")
                snapshot = json.loads(_request(runtime.web_port, "/api/web/events", headers=bearer)[2])
                self.assertTrue(snapshot["events"])
                self.assertTrue(snapshot["events"][0]["event_id"].startswith("web:"))

                status, _headers, body = _request(
                    runtime.web_port,
                    "/api/web/run-preferences",
                    method="POST",
                    body=_json_bytes({"run_preferences": {"domains": ["example.test"]}}),
                    headers={**bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["run_preferences"]["domains"], ["example.test"])

                save = {"scope": "finder", "name": "a4-list", "kind": "user", "domains": ["example.test"]}
                status, _headers, body = _request(
                    runtime.web_port,
                    "/api/web/presets/save",
                    method="POST",
                    body=_json_bytes(save),
                    headers={**bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)
                status, _headers, body = _request(
                    runtime.web_port,
                    "/api/web/presets/delete-user-lists",
                    method="POST",
                    body=_json_bytes({"scope": "finder", "names": ["a4-list"]}),
                    headers={**bearer, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, body)

                self.assertEqual(_request(runtime.web_port, "/api/internal/web-views/status", headers=bearer)[0], 404)
                self.assertEqual(_request(runtime.web_port, "/api/internal/web-views/status")[0], 401)
                self.assertEqual(
                    _request(
                        runtime.web_port,
                        "/api/internal/web-views/status",
                        method="HEAD",
                        headers=bearer,
                    )[0],
                    404,
                )
                self.assertEqual(_request(runtime.core_port, "/api/internal/web-views/status", headers=bearer)[0], 200)
                self.assertEqual(_request(runtime.core_port, "/api/internal/web-views/status")[0], 401)
                status, headers, body = _request(
                    runtime.core_port,
                    "/api/internal/web-views/status",
                    method="HEAD",
                    headers=bearer,
                )
                self.assertEqual((status, headers.get("content-type"), body), (200, "application/json; charset=utf-8", b""))
                self.assertEqual(
                    _request(
                        runtime.core_port,
                        "/api/internal/web-views/status",
                        method="POST",
                        body=b"{}",
                        headers={**bearer, "Content-Type": "application/json"},
                    )[0],
                    404,
                )
                self.assertEqual(
                    _request(
                        runtime.web_port,
                        "/api/internal/web-views/status",
                        method="POST",
                        body=b"{}",
                        headers={**bearer, "Content-Type": "application/json"},
                    )[0],
                    404,
                )
                self.assertEqual(_request(runtime.core_port, "/api/web/status", headers=bearer)[0], 404)
                self.assertEqual(_request(runtime.web_port, "/api/not-a-route")[0], 401)
                self.assertEqual(_request(runtime.web_port, "/api/not-a-route", headers=bearer)[0], 404)
                contract = _request(runtime.core_port, "/openapi.json")[2]
                self.assertNotIn(b"/api/internal/", contract)

            finally:
                runtime.close(self)

    def test_web_keeps_static_shell_when_core_is_down_and_retries_core_503(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state_dir = Path(raw) / "state"
            runtime = _SplitRuntime(state_dir)
            runtime.start(self)
            try:
                token = _login(self, runtime.web_port)
                bearer = {"Authorization": f"Bearer {token}"}
                runtime.force_view_503.set()
                status, _headers, body = _request(runtime.web_port, "/api/web/run-preferences", headers=bearer)
                self.assertEqual(status, 503, body)
                self.assertEqual(json.loads(body)["error"]["code"], "storage_unavailable")
                runtime.force_view_503.clear()
                self.assertEqual(_request(runtime.web_port, "/api/web/run-preferences", headers=bearer)[0], 200)
            finally:
                runtime.close(self)

            down_web_port = _free_port()
            context = multiprocessing.get_context("spawn")
            ready, stop, errors = context.Event(), context.Event(), context.Queue()
            process = context.Process(
                target=_serve_web_process,
                args=(str(state_dir), down_web_port, f"http://127.0.0.1:{_free_port()}", ready, stop, errors),
                name="a4-web-core-down-no-db",
            )
            process.start()
            try:
                self.assertTrue(ready.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                self.assertEqual(_request(down_web_port, "/")[0], 200)
                self.assertEqual(_request(down_web_port, "/api/web/status")[0], 502)
            finally:
                stop.set()
                process.join(timeout=_PROCESS_TIMEOUT_SECONDS)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                    self.fail("core-down Web process did not stop")
                self.assertEqual(process.exitcode, 0)
                try:
                    error = errors.get_nowait()
                except queue.Empty:
                    error = None
                self.assertIsNone(error, error)

    def test_split_browser_bootstrap_retries_after_a_real_core_503(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _SplitRuntime(Path(raw) / "state")
            runtime.start(self)
            runtime.force_view_503.set()
            try:
                with PlaywrightPage() as page:
                    page.navigate(f"http://127.0.0.1:{runtime.web_port}/")
                    page.wait_for(
                        "document.readyState === 'complete' && document.querySelector('input[autocomplete=username]')",
                        "split Web serves the login shell while Core views return 503",
                    )
                    page.fill("input[autocomplete=username]", "admin")
                    page.fill("input[autocomplete=current-password]", "admin")
                    page.click('form button')
                    page.wait_for(
                        "[...document.querySelectorAll('[role=status]')].some(e=>e.textContent==='Не удалось загрузить Core') && [...document.querySelectorAll('button')].some(e=>e.textContent==='Повторить загрузку' && !e.disabled)",
                        "Core 503 reaches the browser bootstrap failure state",
                    )
                    runtime.force_view_503.clear()
                    page.click("button:text-is(\"Повторить загрузку\")")
                    page.wait_for(
                        "!!document.querySelector('[role=tablist]') && !!document.querySelector('[aria-label=Сводка]')",
                        "browser bootstrap retries through the split Web-to-Core path",
                    )
                    self.assertEqual(
                        page.evaluate(
                            "({ token: Boolean(localStorage.getItem('gp-control-plane-auth-token')), "
                            "status: document.querySelector('[aria-label=Сводка] button .metric-value').textContent })"
                        ),
                        {"token": False, "status": "Свободно"},
                    )
            finally:
                runtime.close(self)

    def test_split_sse_established_frame_closes_after_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _SplitRuntime(Path(raw) / "state")
            runtime.start(self)
            connection = response = None
            try:
                old_token = _login(self, runtime.web_port)
                old_bearer = {"Authorization": f"Bearer {old_token}"}
                connection, response = _open_sse(runtime.web_port, old_bearer)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.readline(), b"event: status\n")
                self.assertTrue(response.readline().startswith(b"data: "))
                self.assertEqual(response.readline(), b"\n")
                new_token = _change_password(self, runtime.core_port, old_bearer)
                remainder = response.read()
                self.assertNotIn(b"HTTP/", remainder)
                self.assertEqual(response.read(), b"")
                _close_sse(connection, response)
                connection = response = None
                self.assertEqual(_request(runtime.web_port, "/api/web/events", headers=old_bearer)[0], 401)
                self.assertEqual(
                    _request(runtime.web_port, "/api/web/events", headers={"Authorization": f"Bearer {new_token}"})[0], 200
                )
            finally:
                _close_sse(connection, response)
                runtime.close(self)

    def test_split_sse_before_first_frame_race_is_separate_from_established_stream(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = _SplitRuntime(Path(raw) / "state", sse_gate=True)
            runtime.start(self)
            connection = response = None
            try:
                old_token = _login(self, runtime.web_port)
                old_bearer = {"Authorization": f"Bearer {old_token}"}
                connection, response = _open_sse(runtime.web_port, old_bearer)
                self.assertEqual(response.status, 200)
                self.assertTrue(runtime.stream_entered.wait(timeout=_PROCESS_TIMEOUT_SECONDS))
                new_token = _change_password(self, runtime.core_port, old_bearer)
                runtime.stream_release.set()
                self.assertEqual(response.read(), b"")
                _close_sse(connection, response)
                connection = response = None
                self.assertEqual(_request(runtime.web_port, "/api/web/events", headers=old_bearer)[0], 401)
                self.assertEqual(
                    _request(runtime.web_port, "/api/web/events", headers={"Authorization": f"Bearer {new_token}"})[0], 200
                )
            finally:
                _close_sse(connection, response)
                runtime.close(self)


def _login(test: unittest.TestCase, port: int) -> str:
    status, _headers, body = _request(
        port,
        "/api/auth/login",
        method="POST",
        body=_json_bytes({"username": "admin", "password": "admin"}),
        headers={"Content-Type": "application/json"},
    )
    test.assertEqual(status, 200, body)
    return str(json.loads(body)["access_token"])


def _change_password(test: unittest.TestCase, port: int, bearer: dict[str, str]) -> str:
    status, _headers, body = _request(
        port,
        "/api/auth/change-password",
        method="POST",
        body=_json_bytes({"current_password": "admin", "new_password": "a4-split-password"}),
        headers={**bearer, "Content-Type": "application/json"},
    )
    test.assertEqual(status, 200, body)
    return str(json.loads(body)["access_token"])


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


def _open_sse(port: int, headers: dict[str, str]) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/api/web/events/stream", headers=headers)
    return connection, connection.getresponse()


def _close_sse(connection: http.client.HTTPConnection | None, response: http.client.HTTPResponse | None) -> None:
    try:
        if response is not None:
            response.close()
    finally:
        if connection is not None:
            connection.close()


def _wait_for_health(test: unittest.TestCase, port: int) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if _request(port, "/api/health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    test.fail(f"listener on {port} did not become healthy")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
