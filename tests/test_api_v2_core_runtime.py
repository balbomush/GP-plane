"""Production listener mounts API 2, its schema, and the migrated storage."""

from contextlib import ExitStack, closing
from http.client import HTTPConnection
from pathlib import Path
import json
import socket
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from gp_control_plane.config import build_config
from gp_control_plane.http.core_runtime import create_core_runtime
from gp_control_plane.state import write_state
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

    def test_ambiguous_request_framing_cannot_dispatch_a_pipelined_request(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as client:
            client.sendall(
                b"POST /api/v2/access/authentication HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n"
                b"\r\n0\r\n\r\nGET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            parts = []
            while data := client.recv(65536):
                parts.append(data)
        response = b"".join(parts)
        self.assertTrue(response.startswith(b"HTTP/1.1 400"))
        self.assertEqual(response.count(b"HTTP/1.1 "), 1)
        self.assertIn(b"connection: close", response.lower())
        self.assertIn(b'"code":"invalid_request"', response)

    def test_sse_storage_error_before_headers_matches_regular_api(self):
        from gp_control_plane.storage import StorageUnavailableError

        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "migration-fixture-only",
        })[2]["bearer_token"]
        with patch("gp_control_plane.application.access.require_access",
                   side_effect=StorageUnavailableError("synthetic-private-sentinel")):
            for route in ("current", "events"):
                status, _, payload = self.call("GET", f"/api/v2/search/{route}", b"", token)
                self.assertEqual((status, payload["error"]["code"]), (503, "storage_unavailable"))
                self.assertNotIn("synthetic-private-sentinel", json.dumps(payload))

    def test_restart_requires_proof_for_a_quarantined_process_even_if_nft_is_gone(self):
        with TemporaryDirectory() as root:
            state = Path(root) / "restart-state"
            write_state(state, {
                "current_run_id": "fixture", "current_run_status": "quarantined",
                "current_run_nft_table": "blockcheck123",
            })
            with patch("gp_control_plane.http.core_runtime.recover_registered_process_runs", return_value=True) as broad, patch(
                "gp_control_plane.http.core_runtime.cleanup_owned_nft_table",
            ) as cleanup, patch("gp_control_plane.http.core_runtime.recover_quarantined_process_run",
                                side_effect=RuntimeError("run proof is missing")) as recover:
                with self.assertRaisesRegex(RuntimeError, "run proof is missing"):
                    create_core_runtime(build_config(state), "127.0.0.1", self.port)
                recover.assert_called_once_with("fixture")
                broad.assert_not_called()
                cleanup.assert_not_called()

    def test_update_validation_collects_independent_uuid_version_and_query_errors(self):
        base = "/api/v2"
        _, _, login = self.call("POST", base + "/access/authentication",
                                {"username": "admin", "password": "migration-fixture-only"})
        status, _, response = self.call("POST", base + "/service/updates?unexpected=1",
                                        {"update_id": "invalid uuid"}, login["bearer_token"])
        self.assertEqual(status, 400)
        self.assertEqual({(row["field"], row["code"]) for row in response["error"]["details"]}, {
            ("body:/update_id", "invalid_value"), ("body:/version", "required"),
            ("query:unexpected", "unknown_parameter"),
        })

    def test_shared_sse_releases_its_two_slots_after_revocation(self):
        base = "/api/v2"
        credentials = {"username": "admin", "password": "migration-fixture-only"}
        status, _, login = self.call("POST", base + "/access/authentication", credentials)
        self.assertEqual(status, 200)
        token = login["bearer_token"]

        def connect(cleanup, bearer):
            connection = cleanup.enter_context(closing(HTTPConnection("127.0.0.1", self.port, timeout=10)))
            connection.request("GET", base + "/search/events", headers={"Authorization": "Bearer " + bearer})
            return connection, cleanup.enter_context(closing(connection.getresponse()))

        def first_snapshot(stream):
            self.assertEqual(stream.status, 200)
            self.assertEqual(stream.readline(), b"event: search\n")
            self.assertEqual(json.loads(stream.readline().removeprefix(b"data: ")),
                             {"active": False, "run": None})
            self.assertEqual(stream.readline(), b"\n")

        with ExitStack() as cleanup:
            first_client, first = connect(cleanup, token)
            _, second = connect(cleanup, token)
            first_snapshot(first)
            first_snapshot(second)
            _, rejected = connect(cleanup, token)
            self.assertEqual(rejected.status, 503)
            self.assertEqual(json.load(rejected)["error"]["code"], "stream_capacity_exhausted")
            first.close()
            first_client.close()
            deadline = monotonic() + 7
            while monotonic() < deadline:
                _, replacement = connect(cleanup, token)
                if replacement.status == 200:
                    first_snapshot(replacement)
                    break
                self.assertEqual(replacement.status, 503)
                self.assertEqual(json.load(replacement)["error"]["code"], "stream_capacity_exhausted")
                sleep(0.1)
            else:
                self.fail("SSE disconnect did not release its permit")
            status, _, _ = self.call("PUT", base + "/access/change_password", {
                "username": "admin", "old_password": credentials["password"], "new_password": "new-fixture-only",
            }, token)
            self.assertEqual(status, 204)
            self.assertEqual(replacement.read(), b"")
            self.assertEqual(second.read(), b"")
            status, _, login = self.call("POST", base + "/access/authentication", {
                **credentials, "password": "new-fixture-only",
            })
            self.assertEqual(status, 200)
            _, next_stream = connect(cleanup, login["bearer_token"])
            first_snapshot(next_stream)
