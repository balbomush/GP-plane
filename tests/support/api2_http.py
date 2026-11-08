"""Loopback API fixture using the installed HTTP transport classes."""

import http.client
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time

from bottle import Bottle
from gp_control_plane.http.http_runtime import WebHttpRuntime


class Api2HttpFixture:
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state"
        app = Bottle()
        self.configure(app)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        runtime = WebHttpRuntime(app, "127.0.0.1", self.port)
        thread = threading.Thread(target=runtime.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            runtime.stop()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def call(self, method, path, value, token=None, media_type="application/json"):
        headers = {"Content-Type": media_type}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, value if isinstance(value, bytes) else json.dumps(value), headers)
            response = conn.getresponse()
            body = response.read()
            is_json = response.headers.get("Content-Type", "").startswith("application/json")
            return response.status, response.headers, json.loads(body) if body and is_json else body or None
        finally:
            conn.close()
