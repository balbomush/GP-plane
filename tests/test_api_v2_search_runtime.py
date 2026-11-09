"""Run acceptance, Stop selection, and GP checkpoint on the HTTP controller."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from http.client import HTTPConnection
import json
from threading import Event, current_thread
from uuid import uuid4
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict

from gp_control_plane.application.api2_search import SearchController
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http.api_v2.app import create_api2_app
from gp_control_plane.http.api_v2.search_input import validate_start
from gp_control_plane.http.asgi import CoreHttpServer
from gp_control_plane.storage import StorageUnavailableError, connect_api2, open_api2
from tests.support.api2_http import Api2HttpFixture


class ControlledEngine:
    def __init__(self):
        self.executions = []
        self.saved = Event()
        self.wait_for_stop_seconds = 5

    def prepare(self, code, parameters):
        return "fixture-v1"

    def run(self, job):
        self.executions.append(job.checkpoint)
        if len(self.executions) == 1:
            job.check("a" * 64, "tcp", "--filter-tcp=443", {}, {"test.example": True})
            job.log("first strategy")
            self.saved.set()
            if not job.cancel.wait(self.wait_for_stop_seconds):
                raise RuntimeError("stop not delivered")
            return "stopped"
        job.check("b" * 64, "tcp", "--filter-tcp=80", {}, {"test.example": True})
        return "completed"


class FixtureStart(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    passes: int


class Api2SearchRuntimeTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        pass

    def create_runtime(self, app):
        self.engine = ControlledEngine()
        with connect_api2(self.state):
            pass
        admission = OperationAdmission()
        self.search = SearchController(self.state, admission, self.engine)
        app = create_api2_app(self.state, admission=admission, search=self.search)
        baseline = create_api2_app(self.state, search=self.search)
        self.base_contract = baseline.openapi()

        @app.post("/api/v2/engines/fixture/start")
        def start_fixture(params: FixtureStart):
            return self.search.start("fixture", {
                "domain_list_id": None, "domains": ["test.example"], "parameters": params.model_dump(),
            })

        self.app = app
        return CoreHttpServer(app, "127.0.0.1", self.port)

    def test_an_isolated_adapter_extends_only_its_own_start(self):
        actual = self.app.openapi()
        for path in ("/engines/blockcheck2/start", "/engines/blockcheck2-gp/start", "/search/current", "/search/stop"):
            self.assertEqual(actual["paths"][path], self.base_contract["paths"][path])
        self.assertIn("/engines/fixture/start", actual["paths"])
        self.assertIn("passes", actual["components"]["schemas"]["FixtureStart"]["properties"])
        with open_api2(self.state) as conn:
            conn.execute("INSERT INTO engines(id, code, name) VALUES(99,'fixture','Fixture')")
        _, _, login = self.call("POST", "/api/v2/access/authentication", {"username": "admin", "password": "admin"})
        token = login["bearer_token"]
        with patch.dict("gp_control_plane.application.api2_search._KINDS", fixture="fixture-only"):
            status, _, accepted = self.call("POST", "/api/v2/engines/fixture/start", {"passes": 4}, token)
            self.assertEqual(status, 200)
            self.assertTrue(self.engine.saved.wait(3))
            self.assertEqual(self.search.active.parameters, {"passes": 4})
            _, _, current = self.call("GET", "/api/v2/search/current", b"", token)
            self.assertEqual(current["run"]["engine"]["code"], "fixture")
            status, _, stopped = self.call("POST", "/api/v2/search/stop", b"", token)
            self.assertEqual((status, stopped["run"]["id"], stopped["run"]["status"]),
                             (200, accepted["id"], "stopped"))

    def test_revoke_and_slow_sse_do_not_stop_active_search(self):
        self.engine.wait_for_stop_seconds = 30
        base = "/api/v2"
        credentials = {"username": "admin", "password": "admin"}
        token = self.call("POST", base + "/access/authentication", credentials)[2]["bearer_token"]
        status, _, accepted = self.call("POST", base + "/engines/blockcheck2-gp/start",
                                        {"domains": ["test.example"]}, token)
        self.assertEqual(status, 200)
        self.assertTrue(self.engine.saved.wait(3))

        with ExitStack() as stack:
            def stream(bearer):
                conn = stack.enter_context(closing(HTTPConnection("127.0.0.1", self.port, timeout=5)))
                conn.request("GET", base + "/search/events", headers={"Authorization": "Bearer " + bearer})
                return stack.enter_context(closing(conn.getresponse()))

            first, slow = stream(token), stream(token)
            self.assertEqual((first.status, slow.status), (200, 200))
            self.assertEqual(first.readline(), b"event: search\n")
            self.assertIn(accepted["id"].encode(), first.readline())
            denied = stream(token)
            self.assertEqual(denied.status, 503)
            self.assertEqual(json.load(denied)["error"]["code"], "stream_capacity_exhausted")
            status, _, _ = self.call("PUT", base + "/access/change_password", {
                "username": "admin", "old_password": "admin", "new_password": "changed",
            }, token)
            self.assertEqual(status, 204)
            first.read()
            slow.read()
            token = self.call("POST", base + "/access/authentication",
                              {**credentials, "password": "changed"})[2]["bearer_token"]
            self.assertEqual(stream(token).status, 200)
            _, _, current = self.call("GET", base + "/search/current", b"", token)
            self.assertEqual((current["active"], current["run"]["id"]), (True, accepted["id"]))
            status, _, stopped = self.call("POST", base + "/search/stop", b"", token)
            self.assertEqual((status, stopped["run"]["status"]), (200, "stopped"))

    def test_shutdown_cancels_engine_when_final_write_is_unavailable(self):
        base = "/api/v2"
        _, _, login = self.call("POST", base + "/access/authentication", {"username": "admin", "password": "admin"})
        status, _, _ = self.call("POST", base + "/engines/blockcheck2-gp/start",
                                 {"domains": ["test.example"]}, login["bearer_token"])
        self.assertEqual(status, 200)
        self.assertTrue(self.engine.saved.wait(3))
        job = self.search.active

        def fault(state):
            if current_thread().name == "api2-search":
                raise StorageUnavailableError("shutdown-secret-sentinel")
            return open_api2(state)

        with patch("gp_control_plane.application.api2_search.open_api2", side_effect=fault):
            with self.assertLogs("gp_control_plane", level="DEBUG") as observed:
                self.runtime.stop()
                self.server_thread.join(5)
                self.assertFalse(self.server_thread.is_alive())
            self.assertTrue(job.cancel.is_set())
            self.assertTrue(job.done.is_set())
        self.assertTrue(any("error_code=finalize_failed" in line for line in observed.output))
        self.assertTrue(any("error_code=cleanup_unverified" in line for line in observed.output))
        self.assertTrue(any("traceback=" in line for line in observed.output))
        self.assertFalse(any("shutdown-secret-sentinel" in line for line in observed.output))

    def test_stop_retains_committed_check_and_resume_passes_saved_position(self):
        prefix = "/api/v2"
        status, _, login = self.call("POST", prefix + "/access/authentication",
                                     {"username": "admin", "password": "admin"})
        self.assertEqual(status, 200)
        token = login["bearer_token"]
        status, _, started = self.call("POST", prefix + "/engines/blockcheck2-gp/start",
                                       {"domains": ["test.example"]}, token)
        self.assertEqual(status, 200)
        run_id = started["id"]
        self.assertTrue(self.engine.saved.wait(3))
        status, _, current = self.call("GET", prefix + "/search/current", b"", token)
        self.assertEqual((status, current["run"]["progress"]["checked"]), (200, 1))
        status, _, log = self.call("GET", prefix + f"/data/runs/{run_id}/log", b"", token)
        self.assertEqual((status, log["entries"][-1]["message"]), (200, "first strategy"))
        status, _, stop = self.call("POST", prefix + "/search/stop", b"", token)
        self.assertEqual((status, stop["run"]["status"], stop["run"]["id"]), (200, "stopped", run_id))
        self.assertIsNotNone(stop["run"]["finished_at"])
        _, _, history = self.call("GET", prefix + f"/data/runs/{run_id}", b"", token)
        self.assertTrue(history["resume_available"])
        status, _, resumed = self.call("POST", prefix + f"/engines/blockcheck2-gp/runs/{run_id}/resume", b"", token)
        self.assertEqual((status, resumed["id"]), (200, run_id))
        deadline = monotonic() + 3
        while monotonic() < deadline:
            _, _, history = self.call("GET", prefix + f"/data/runs/{run_id}", b"", token)
            if history["status"] == "completed":
                break
            sleep(0.02)
        self.assertEqual(history["status"], "completed")
        self.assertEqual(self.engine.executions[1]["trail"], [["a" * 64, True]])

        # Restart loses the live buffer but a diagnostic run has a saved tail.
        diagnostics = self.state / "strategy-finder" / "diagnostics"
        diagnostics.mkdir()
        (diagnostics / f"{run_id}.log").write_bytes(b"before restart\nafter restart\n")
        self.search.logs.clear()
        path = prefix + f"/data/runs/{run_id}/log"
        status, _, saved = self.call("GET", path + "?limit=2", b"", token)
        self.assertEqual(status, 200)
        self.assertEqual([entry["message"] for entry in saved["entries"]], ["before restart", "after restart"])
        status, _, following = self.call("GET", path + "?after=" + saved["entries"][0]["position"], b"", token)
        self.assertEqual(status, 200)
        self.assertEqual([entry["message"] for entry in following["entries"]], ["after restart"])
        self.assertFalse(following["truncated"])
        (diagnostics / f"{run_id}.log").write_bytes(b"".join(f"entry {i}\n".encode() for i in range(60)))
        self.assertEqual(len(self.call("GET", path, b"", token)[2]["entries"]), 50)

    def test_stop_reply_keeps_its_execution_when_the_same_id_resumes(self):
        base = "/api/v2"
        _, _, login = self.call("POST", base + "/access/authentication",
                                {"username": "admin", "password": "admin"})
        token = login["bearer_token"]
        status, _, accepted = self.call("POST", base + "/engines/blockcheck2-gp/start",
                                        {"domains": ["test.example"]}, token)
        self.assertEqual(status, 200)
        self.assertTrue(self.engine.saved.wait(3))
        prior = self.search.active
        entered, release = Event(), Event()
        summary = self.search._summary

        def hold_reply(execution):
            if execution is prior and prior.done.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("stop reply was not released")
            return summary(execution)

        with patch.object(self.search, "_summary", side_effect=hold_reply), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.call, "POST", base + "/search/stop", b"", token)
            try:
                self.assertTrue(entered.wait(3))
                status, _, resumed = self.call("POST", base + f"/engines/blockcheck2-gp/runs/{accepted['id']}/resume",
                                               b"", token)
                self.assertEqual((status, resumed["id"]), (200, accepted["id"]))
            finally:
                release.set()
            status, _, stopped = pending.result(5)
        self.assertEqual((status, stopped["run"]["status"], stopped["run"]["id"]),
                         (200, "stopped", accepted["id"]))
        self.assertEqual(stopped["run"]["progress"]["checked"], 1)
        active = self.search.active
        if active is not None:
            self.assertTrue(active.done.wait(3))
        self.assertEqual(self.call("GET", base + f"/data/runs/{accepted['id']}", b"", token)[2]["status"],
                         "completed")

    def test_unconfirmed_cleanup_protects_run_and_milestone(self):
        from gp_control_plane.jobs import ManagedRuntimeQuarantinedError

        base = "/api/v2"
        _, _, login = self.call("POST", base + "/access/authentication",
                                {"username": "admin", "password": "admin"})
        token = login["bearer_token"]

        def fail_cleanup(job):
            raise ManagedRuntimeQuarantinedError("synthetic unconfirmed cleanup")

        self.engine.run = fail_cleanup
        status, _, accepted = self.call("POST", base + "/engines/blockcheck2/start",
                                        {"domains": ["test.example"]}, token)
        self.assertEqual(status, 200)
        active = self.search.active
        self.assertTrue(active.done.wait(3))
        run_id = accepted["id"]
        self.assertEqual(self.call("GET", base + "/search/current", b"", token)[2]["run"]["status"], "stopping")
        self.assertEqual(self.call("GET", base + f"/data/runs/{run_id}", b"", token)[2]["status"], "stopping")
        self.assertEqual(self.call("DELETE", base + f"/data/runs/{run_id}", b"", token)[0], 409)
        self.assertEqual(self.call("POST", base + "/data/milestones", b"", token)[0], 409)
        self.assertEqual(self.call("POST", base + "/search/stop", b"", token)[0], 503)

    def test_start_with_unknown_domain_list_is_not_found(self):
        base = "/api/v2"
        _, _, login = self.call("POST", base + "/access/authentication",
                                {"username": "admin", "password": "admin"})
        for engine in ("blockcheck2", "blockcheck2-gp"):
            status, _, result = self.call("POST", base + f"/engines/{engine}/start", {
                "domains": ["test.example"], "domain_list_id": 999,
            }, login["bearer_token"])
            self.assertEqual((status, result["error"]["code"]), (404, "not_found"))
        self.assertEqual(self.engine.executions, [])

    def test_resume_rechecks_milestone_after_environment_preparation(self):
        prefix = "/api/v2"
        _, _, login = self.call("POST", prefix + "/access/authentication",
                                {"username": "admin", "password": "admin"})
        token = login["bearer_token"]
        run_id = uuid4().hex
        spec, errors = validate_start("blockcheck2-gp", {"domains": ["test.example"]})
        self.assertFalse(errors)
        with connect_api2(self.state) as conn:
            conn.execute(
                """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json, checkpoint_json)
                   VALUES(?, 1, 2, 'stopped', ?, ?, ?)""",
                (run_id, json.dumps(spec["parameters"]), json.dumps(spec["domains"]),
                 json.dumps({"revision": "fixture-v1", "trail": [["a" * 64, True]]})),
            )
            before = tuple(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        prepared, release = Event(), Event()

        def wait_for_test(code, parameters):
            prepared.set()
            if not release.wait(5):
                raise RuntimeError("readiness fixture timed out")
            return "fixture-v1"

        self.engine.prepare = wait_for_test
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.call, "POST",
                                     prefix + f"/engines/blockcheck2-gp/runs/{run_id}/resume", b"", token)
            try:
                self.assertTrue(prepared.wait(3))
                self.assertEqual(self.call("POST", prefix + "/data/milestones", b"", token)[2], {"id": 2})
            finally:
                release.set()
            status, _, body = future.result(5)
        self.assertEqual((status, body["error"]["code"]), (409, "milestone_conflict"))
        with connect_api2(self.state) as conn:
            self.assertEqual(tuple(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), before)
        self.assertEqual(self.engine.executions, [])
