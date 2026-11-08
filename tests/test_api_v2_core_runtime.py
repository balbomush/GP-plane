"""Production listener mounts API 2, its schema, and the migrated storage."""

from contextlib import closing
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
