"""Status and package preparation use the same heavy-operation admission."""

from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from gp_control_plane import releases
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


ROOT = "/api/v2/service"
TAG = "v0.5.0"
SOURCE = "core-" + "a" * 40 + ".tar.gz"
WHEEL = "gp_access_control_plane-0.5.0-py3-none-any.whl"
SDIST = "gp_access_control_plane-0.5.0.tar.gz"
FILES = {SOURCE: b"source", WHEEL: b"wheel", SDIST: b"sdist"}
CHECKSUMS = "".join(f"{sha256(data).hexdigest()}  {name}\n" for name, data in FILES.items()).encode()
DELIVERY = {**FILES, "SHA256SUMS": CHECKSUMS}


class ServiceTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.admission = OperationAdmission()
        self.configured = hasattr(api_v2, "register_service")
        if self.configured:
            api_v2.register_service(app, self.state, admission=self.admission)

    def test_release_download_checks_exact_supply_and_busy_before_network(self):
        self.assertTrue(self.configured, "service routes are missing")
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        row = {
            "tag_name": TAG, "prerelease": True, "draft": False,
            "assets": [{"name": name, "size": len(data)} for name, data in DELIVERY.items()],
        }
        with patch.object(releases, "fetch_github_releases", return_value=json.dumps([row])) as fetch:
            with self.admission.claim("search"):
                status = self.call("GET", ROOT + "/status", b"", token)[2]
                self.assertEqual((status["busy"], status["operation"]), (True, "search"))
                status, _, result = self.call("POST", ROOT + "/download", {"version": "0.5.0"}, token)
                self.assertEqual((status, result["error"]["code"]), (409, "busy"))
                self.assertEqual(result["error"]["details"], [{"operation": "search"}])
                fetch.assert_not_called()
            self.assertEqual(self.call("GET", ROOT + "/releases?channel=prerelease", b"", token)[2], {
                "channel": "prerelease", "items": [{"tag": TAG, "version": "0.5.0", "prerelease": True}],
            })
            with patch.object(releases, "fetch_release_asset", side_effect=lambda tag, name, size: DELIVERY[name]):
                status, _, result = self.call("POST", ROOT + "/download", {"version": "0.5.0"}, token)
            self.assertEqual((status, result), (200, {"version": "0.5.0", "size_bytes": sum(map(len, FILES.values()))}))
        with patch.object(releases, "fetch_github_releases", side_effect=OSError("offline")) as fetch:
            self.assertEqual(self.call("POST", ROOT + "/download", {"version": "0.5.0"}, token)[0], 200)
            fetch.assert_not_called()
