"""Exercise the API 2 access routes over Core's Bottle/Cheroot transport."""

from datetime import datetime, timezone
import json
import unittest

from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


LOGIN = "/api/v2/access/authentication"
PASSWORD = "/api/v2/access/change_password"


class Api2AccessTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)

    def login(self, password="admin"):
        status, headers, body = self.call("POST", LOGIN, {"username": "admin", "password": password})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(set(body), {"bearer_token", "expires_at"})
        self.assertTrue(body["expires_at"].endswith("Z"))
        expires = datetime.fromisoformat(body["expires_at"])
        self.assertAlmostEqual((expires - datetime.now(timezone.utc)).total_seconds(), 604800, delta=2)
        return body["bearer_token"]

    def test_login_and_password_change_revoke_all_prior_tokens(self):
        first, second = self.login(), self.login()
        self.assertNotEqual(first, second)
        payload = {"username": "admin", "old_password": "admin", "new_password": "changed"}
        status, headers, body = self.call("PUT", PASSWORD, {**payload, "username": "other"}, first)
        self.assertEqual((status, body["error"]["code"]), (403, "invalid_credentials"))
        self.assertIsNone(headers.get("WWW-Authenticate"))
        status, _, body = self.call("PUT", PASSWORD, payload, first)
        self.assertEqual((status, body), (204, None))
        for token in (first, second):
            status, headers, body = self.call("PUT", PASSWORD, payload, token)
            self.assertEqual((status, body["error"]["code"]), (401, "token_revoked"))
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        status, _, body = self.call("POST", LOGIN, {"username": "admin", "password": "admin"})
        self.assertEqual((status, body["error"]["code"]), (401, "invalid_credentials"))
        self.login("changed")

    def test_access_precedes_body_and_all_independent_fields_are_reported(self):
        status, headers, body = self.call("PUT", PASSWORD, b"{")
        self.assertEqual((status, body["error"]["code"]), (401, "token_invalid"))
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")
        self.assertFalse((self.state / "strategy-finder" / "state.sqlite3").exists())
        token = self.login()
        status, _, body = self.call("PUT", PASSWORD + "?extra=1", {
            "username": "", "old_password": 7, "wrong": "synthetic",
        }, token)
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_parameters"))
        self.assertEqual({(item["field"], item["code"]) for item in body["error"]["details"]}, {
            ("body:/username", "empty"), ("body:/old_password", "invalid_type"),
            ("body:/new_password", "required"), ("body:/wrong", "unknown_parameter"),
            ("query:extra", "unknown_parameter"),
        })
        self.assertNotIn("synthetic", json.dumps(body))
        self.login()
