"""Production listener mounts API 2, its schema, and the migrated storage."""

from contextlib import ExitStack, closing
from http.client import HTTPConnection
from pathlib import Path
import json
import sqlite3
import unittest

from gp_control_plane.config import build_config
from gp_control_plane.http.core_runtime import create_core_runtime
from gp_control_plane.storage import db_path
from tests.support.api2_http import Api2HttpFixture


class Api2CoreRuntimeTests(Api2HttpFixture, unittest.TestCase):
    def create_runtime(self, app):
        path = db_path(self.state)
        fixture = Path(__file__).parent / "fixtures" / "api_v2" / "legacy_v11.sql"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(fixture.read_text(encoding="utf-8"))
            auth = json.loads(conn.execute("SELECT value_json FROM app_settings WHERE key='auth'").fetchone()[0])
            self.expected_version = auth["token_version"]
        finally:
            conn.close()
        return create_core_runtime(build_config(self.state), "127.0.0.1", self.port)

    def configure(self, app):
        pass

    def test_standard_listener_migrates_and_exposes_api2(self):
        with closing(sqlite3.connect(db_path(self.state))) as conn:
            self.assertEqual(conn.execute("SELECT token_version FROM access_state").fetchone()[0],
                             self.expected_version)
        status, _, spec = self.call("GET", "/openapi.json", b"")
        self.assertEqual(status, 200)
        self.assertEqual(spec["info"]["version"], "2.0.0-draft.33")
        self.assertEqual(sum(len(methods) for methods in spec["paths"].values()), 41)
        for path, methods in spec["paths"].items():
            for method in methods:
                if path == "/access/authentication":
                    continue
                concrete = path
                while "{" in concrete:
                    start, end = concrete.index("{"), concrete.index("}")
                    concrete = concrete[:start] + "fixture" + concrete[end+1:]
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.call(method.upper(), "/api/v2" + concrete, b"")[0], 401)
        self.assertEqual(self.call("GET", "/swagger/", b"")[0], 200)
        self.assertEqual(self.call("POST", "/api/auth/login", {})[0], 404)
        status, _, health = self.call("GET", "/api/health", b"")
        self.assertEqual((status, health), (200, {"status": "ok"}))

    def test_shared_sse_releases_its_two_slots_after_revocation(self):
        base = "/api/v2"
        credentials = {"username": "admin", "password": "migration-fixture-only"}
        status, _, login = self.call("POST", base + "/access/authentication", credentials)
        self.assertEqual(status, 200)
        token = login["bearer_token"]

        def connect(cleanup, bearer):
            connection = cleanup.enter_context(closing(HTTPConnection("127.0.0.1", self.port, timeout=10)))
            connection.request("GET", base + "/search/events", headers={"Authorization": "Bearer " + bearer})
            return cleanup.enter_context(closing(connection.getresponse()))

        def first_snapshot(stream):
            self.assertEqual(stream.status, 200)
            self.assertEqual(stream.readline(), b"event: search\n")
            self.assertEqual(json.loads(stream.readline().removeprefix(b"data: ")),
                             {"active": False, "run": None})
            self.assertEqual(stream.readline(), b"\n")

        with ExitStack() as cleanup:
            first, second = connect(cleanup, token), connect(cleanup, token)
            first_snapshot(first)
            first_snapshot(second)
            rejected = connect(cleanup, token)
            self.assertEqual(rejected.status, 503)
            self.assertEqual(json.load(rejected)["error"]["code"], "stream_capacity_exhausted")
            status, _, _ = self.call("PUT", base + "/access/change_password", {
                "username": "admin", "old_password": credentials["password"], "new_password": "new-fixture-only",
            }, token)
            self.assertEqual(status, 204)
            self.assertEqual(first.read(), b"")
            self.assertEqual(second.read(), b"")
            status, _, login = self.call("POST", base + "/access/authentication", {
                **credentials, "password": "new-fixture-only",
            })
            self.assertEqual(status, 200)
            first_snapshot(connect(cleanup, login["bearer_token"]))
