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



class A6CoreFactoryTests(unittest.TestCase):
    def test_factory_is_unbound_and_headless_does_not_import_renderer(self) -> None:
        import sys

        sys.modules.pop("gp_control_plane.web.ui", None)
        with tempfile.TemporaryDirectory() as raw:
            config = AppConfig(output=OutputConfig(state_dir=Path(raw) / "state"))
            services = CoreServices(config=config, discovery=mock.Mock())
            app = create_core_app(services)

            self.assertTrue(callable(app))
            self.assertNotIn("gp_control_plane.web.ui", sys.modules)
