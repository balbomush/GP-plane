"""Run acceptance, Stop selection, and GP checkpoint on the HTTP controller."""

from threading import Event
from time import monotonic, sleep
import unittest

from gp_control_plane.application.api2_search import SearchController
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http.api_v2.app import create_api2_app
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
