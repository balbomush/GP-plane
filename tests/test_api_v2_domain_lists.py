"""Domain-list HTTP writes preserve order, history and source facts."""

import unittest

from gp_control_plane import storage
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


LISTS = "/api/v2/data/domain-lists"


class DomainListTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.data_configured = hasattr(api_v2, "register_domain_lists")
        if self.data_configured:
            api_v2.register_domain_lists(app, self.state)

    def test_duplicate_create_and_replace_leave_state_unchanged(self):
        self.assertTrue(self.data_configured, "domain list routes are missing")
        _, _, result = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })
        token = result["bearer_token"]
        content = {"name": "New", "domains": [{"domain": "two.example"}, {"domain": "one.example"}]}
        duplicate = {"name": "Changed", "domains": [{"domain": "two.example"}, {"domain": "TWO.example."}]}
        status, _, result = self.call("POST", LISTS, duplicate, token)
        self.assertEqual((status, result["error"]["code"]), (400, "invalid_parameters"))
        self.assertEqual(self.call("GET", LISTS + "?kind=user", b"", token)[2]["total"], 0)

        status, _, result = self.call("POST", LISTS, content, token)
        self.assertEqual(status, 201)
        key = result["id"]
        url = f"{LISTS}/{key}"
        self.assertEqual(result, {"id": key, "name": "New", "kind": "user", "source": None})
        status, _, result = self.call("PUT", url, duplicate, token)
        self.assertEqual((status, result["error"]["code"]), (400, "invalid_parameters"))
        self.assertEqual(self.call("GET", url, b"", token)[2]["name"], "New")
        self.assertEqual(self.call("GET", url + "/domains", b"", token)[2]["items"], content["domains"])

        with storage.connect_api2(self.state) as conn:
            conn.execute(
                """INSERT INTO runs(id, milestone_id, engine_id, status, preset_id, parameters_json, domains_json)
                   VALUES('historical', 1, 1, 'completed', ?, '{}', '["two.example","one.example"]')""",
                (key,),
            )
            builtin = conn.execute(
                "INSERT INTO domain_presets(name, kind) VALUES('fixed', 'system') RETURNING id"
            ).fetchone()[0]
        status, _, result = self.call("PUT", f"{LISTS}/{builtin}", content, token)
        self.assertEqual((status, result["error"]["code"]), (403, "read_only_resource"))
        self.assertEqual(self.call("DELETE", url, b"", token)[0], 204)
        with storage.connect_api2(self.state) as conn:
            row = conn.execute("SELECT preset_id, domains_json FROM runs WHERE id='historical'").fetchone()
            self.assertEqual(tuple(row), (None, '["two.example","one.example"]'))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0], 2)
