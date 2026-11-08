"""Run acceptance, Stop selection, and GP checkpoint on the HTTP controller."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from uuid import uuid4
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from gp_control_plane.application.api2_search import SearchController
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http.api_v2.app import create_api2_app
from gp_control_plane.http.api_v2.search_input import validate_start
from gp_control_plane.http.http_runtime import WebHttpRuntime
from gp_control_plane.storage import connect_api2
from tests.support.api2_http import Api2HttpFixture


class ControlledEngine:
    def __init__(self):
        self.executions = []
        self.saved = Event()

    def prepare(self, code, parameters):
        return "fixture-v1"

    def run(self, job):
        self.executions.append(job.checkpoint)
        if len(self.executions) == 1:
            job.check("a" * 64, "tcp", "--filter-tcp=443", {}, {"test.example": True})
            job.log("first strategy")
            self.saved.set()
            if not job.cancel.wait(5):
                raise RuntimeError("stop not delivered")
            return "stopped"
        job.check("b" * 64, "tcp", "--filter-tcp=80", {}, {"test.example": True})
        return "completed"


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
        return WebHttpRuntime(app, "127.0.0.1", self.port)

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
