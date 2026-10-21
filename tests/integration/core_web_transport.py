"""Callable A6 Core Bottle/Cheroot qualification checks.

These tests deliberately start the production Core and Web factories on real
loopback sockets.  They do not retain a BaseHTTP surrogate for the migrated
Core boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sys



from gp_control_plane.config import AppConfig, OutputConfig
from gp_control_plane.web.core_app import CoreServices, create_core_app
from gp_control_plane.web.core_runtime import create_core_runtime
from tests.support.web_process import start_web_process


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _wait_for_listener(port: int) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise AssertionError(f"listener did not start on {port}")
            time.sleep(0.01)


def _request(
    port: int,
    method: str,
    path: str,
    *,
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


def _login(port: int) -> dict[str, str]:
    status, _headers, body = _request(
        port,
        "POST",
        "/api/auth/login",
        body=b'{"username":"admin","password":"admin"}',
        headers={"Content-Type": "application/json"},
    )
    if status != 200:
        raise AssertionError(body)
    return {"Authorization": f"Bearer {json.loads(body)['access_token']}"}


@contextlib.contextmanager
def _running_split() -> object:
    with tempfile.TemporaryDirectory() as raw:
        config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
        core_port = _free_port()
        web_port = _free_port()
        core = create_core_runtime(config, "127.0.0.1", core_port)
        web = start_web_process(web_port, f"http://127.0.0.1:{core_port}")
        core_thread = threading.Thread(target=core.serve_forever, name="a6-core", daemon=True)
        web_thread = None
        core_thread.start()
        _wait_for_listener(core_port)
        _wait_for_listener(web_port)
        try:
            yield config, core_port, web_port, core, web, core_thread, web_thread
        finally:
            web.close()
            core.stop()
            core_thread.join(timeout=5)
            if core_thread.is_alive():
                raise AssertionError("A6 listener did not stop within 5 seconds")


class A6CoreWebTransportTests(unittest.TestCase):
    def test_real_core_web_route_openapi_head_internal_and_legacy_501_contracts(self) -> None:
        with _running_split() as (_config, core_port, web_port, _core, _web, _core_thread, _web_thread):
            bearer = _login(web_port)
            for port in (core_port, web_port):
                status, headers, body = _request(port, "GET", "/api/core/status", headers=bearer)
                self.assertEqual(status, 200, body)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(int(headers["content-length"]), len(body))
                head_status, head_headers, head_body = _request(port, "HEAD", "/api/health")
                self.assertEqual(head_status, 200)
                self.assertEqual(head_body, b"")
                self.assertEqual(head_headers["content-type"], "application/json; charset=utf-8")

            core_root_status, _headers, core_root = _request(core_port, "GET", "/")
            self.assertEqual(core_root_status, 404)
            self.assertNotIn(b"<!doctype html>", core_root.lower())
            web_root_status, web_root_headers, web_root = _request(web_port, "GET", "/")
            self.assertEqual(web_root_status, 200)
            self.assertEqual(int(web_root_headers["content-length"]), len(web_root))
            self.assertIn(b"<!doctype html>", web_root.lower())

            internal_status, _headers, _body = _request(
                core_port, "GET", "/api/internal/web-views/status", headers=bearer
            )
            self.assertEqual(internal_status, 200)
            denied_status, _headers, denied_body = _request(
                web_port, "GET", "/api/internal/web-views/status", headers=bearer
            )
            self.assertEqual(denied_status, 404, denied_body)
            openapi_status, _headers, openapi_body = _request(core_port, "GET", "/openapi.json")
            self.assertEqual(openapi_status, 200)
            self.assertFalse(any(path.startswith("/api/web/") for path in json.loads(openapi_body)["paths"]))

            put_status, put_headers, put_body = _request(web_port, "PUT", "/api/health")
            self.assertEqual(put_status, 501)
            self.assertNotIn("allow", put_headers)
            self.assertEqual(len(put_body), 356)
            self.assertEqual(put_headers["content-length"], "356")
            self.assertEqual(put_headers["content-type"], "text/html;charset=utf-8")
            self.assertIn(b"Unsupported method ('PUT')", put_body)


    def test_real_core_body_framing_and_established_sse_revoke_close(self) -> None:
        with _running_split() as (_config, core_port, web_port, _core, _web, _core_thread, _web_thread):
            # Public login is intentionally body-framing checked before auth.
            raw = socket.create_connection(("127.0.0.1", core_port), timeout=5)
            try:
                raw.sendall(
                    b"POST /api/auth/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}"
                )
                response = raw.recv(4096)
            finally:
                raw.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 400"), response)

            bearer = _login(web_port)
            # Exercise the migrated Core listener directly on the wire as
            # well as through Web.  A revoked streaming response is a normal
            # HTTP/1.1 terminal response: Cheroot must finish its chunked
            # coding before the Core connection closes.  Treating bare EOF as
            # success here would mask a Web-side IncompleteRead later.
            core_stream = socket.create_connection(("127.0.0.1", core_port), timeout=5)
            core_stream.settimeout(0.25)
            core_payload = b""
            try:
                core_stream.sendall(
                    (
                        "GET /api/internal/web-events/stream HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        f"Authorization: {bearer['Authorization']}\r\n"
                        # The production Core client deliberately closes each
                        # upstream response after relaying it, so reproduce
                        # that exact request-side transport contract.
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                )
                deadline = time.monotonic() + 5
                while b"\n\n" not in core_payload:
                    if time.monotonic() >= deadline:
                        self.fail(f"direct Core SSE did not produce its first frame: {core_payload!r}")
                    try:
                        core_payload += core_stream.recv(4096)
                    except TimeoutError:
                        continue

                stream = http.client.HTTPConnection("127.0.0.1", web_port, timeout=5)
                try:
                    stream.request("GET", "/api/web/events/stream", headers=bearer)
                    reply = stream.getresponse()
                    self.assertEqual(reply.status, 200)
                    frame = reply.readline() + reply.readline() + reply.readline()
                    self.assertIn(b"event:", frame)
                    self.assertTrue(frame.endswith(b"\n"))
                    changed = _request(
                        core_port,
                        "POST",
                        "/api/auth/change-password",
                        body=b'{"current_password":"admin","new_password":"a6-new-password"}',
                        headers={**bearer, "Content-Type": "application/json"},
                    )
                    self.assertEqual(changed[0], 200, changed[2])
                    # Frames already emitted before revocation can be buffered;
                    # the established stream must nevertheless reach terminal EOF
                    # without a second HTTP response or a continuing heartbeat.
                    remainder = reply.read()
                    self.assertNotIn(b"HTTP/", remainder)
                    self.assertEqual(reply.read(), b"")
                finally:
                    stream.close()

                deadline = time.monotonic() + 5
                while b"0\r\n\r\n" not in core_payload:
                    if time.monotonic() >= deadline:
                        self.fail(f"direct Core SSE lacked chunked terminal marker: {core_payload!r}")
                    try:
                        part = core_stream.recv(4096)
                    except TimeoutError:
                        continue
                    if not part:
                        break
                    core_payload += part
                self.assertIn(b"0\r\n\r\n", core_payload)
            finally:
                core_stream.close()
