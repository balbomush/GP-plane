"""Milestones and history obey consistent snapshots and catalog lifetimes."""

import unittest

from gp_control_plane import storage
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


ROOT = "/api/v2/data"


class HistoryTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.configured = hasattr(api_v2, "register_history")
        if self.configured:
            api_v2.register_history(app, self.state, resume_available=lambda row, milestone: False)

    def test_active_history_protects_milestone_and_catalog_outlives_run(self):
        self.assertTrue(self.configured, "history routes are missing")
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        with storage.connect_api2(self.state) as conn:
            conn.execute(
                """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json)
                   VALUES('run-a', 1, 1, 'running', '{"example":1}', '["one.example"]')"""
            )
        strategy_id = storage.save_strategy_check(
            self.state, run_id="run-a", protocol="tls", execution_format="nfqws2",
            args="--filter-tcp=443", files={}, outcomes={"one.example": True}, checkpoint=None,
        )
        run_path = ROOT + "/runs/run-a"
        milestone_path = ROOT + "/milestones"
        self.assertEqual(self.call("POST", milestone_path, b"", token)[0], 409)
        self.assertEqual(self.call("DELETE", run_path, b"", token)[0], 409)
        with storage.connect_api2(self.state) as conn:
            conn.execute("UPDATE runs SET status='completed' WHERE id='run-a'")
        status, _, history = self.call("GET", ROOT + "/runs?engine_code=blockcheck2", b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(history["total"], 1)
        detail = history["items"][0]
        self.assertEqual(detail, self.call("GET", run_path, b"", token)[2])
        self.assertEqual(detail["parameters"], {"example": 1})
        self.assertEqual(detail["domains"], ["one.example"])
        self.assertNotIn("checkpoint_json", detail)
        self.assertEqual(self.call("POST", milestone_path, b"", token)[2], {"id": 2})
        self.assertEqual(self.call("GET", milestone_path, b"", token)[2]["current_id"], 2)
        self.assertEqual(self.call("DELETE", run_path, b"", token)[0], 204)
        self.assertEqual(self.call("GET", run_path, b"", token)[0], 404)
        with storage.connect_api2(self.state) as conn:
            row = conn.execute("SELECT strategy_id, milestone_id FROM strategy_domain_results").fetchone()
            self.assertEqual(tuple(row), (strategy_id, 1))
