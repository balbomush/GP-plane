"""Status and package preparation use the same heavy-operation admission."""

from base64 import urlsafe_b64encode
from hashlib import sha256
from io import BytesIO
from tarfile import TarInfo, open as open_tar
from zipfile import ZipFile
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
def _tar_files(entries):
    result = BytesIO()
    with open_tar(fileobj=result, mode="w:gz") as archive:
        for name, data in entries.items():
            info = TarInfo(name)
            info.size = len(data)
            archive.addfile(info, BytesIO(data))
    return result.getvalue()


metadata = b"Metadata-Version: 2.4\r\nName: gp-access-control-plane\r\nVersion: 0.5.0\r\n"
wheel_bytes = BytesIO()
with ZipFile(wheel_bytes, "w") as wheel:
    info = "gp_access_control_plane-0.5.0.dist-info"
    members = {
        f"{info}/METADATA": metadata, f"{info}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
        "gp_control_plane/__init__.py": b'__version__ = "0.5.0"\n',
        "gp_control_plane/cli.py": b"def main():\n    return 0\n",
        "gp_control_plane/http/openapi.json": b"{}\n",
    }
    for name, content in members.items():
        wheel.writestr(name, content)
    record = "".join(
        f"{name},sha256={urlsafe_b64encode(sha256(content).digest()).decode().rstrip('=')},{len(content)}\n"
        for name, content in members.items()
    )
    wheel.writestr(f"{info}/RECORD", record + f"{info}/RECORD,,\n")
FILES = {
    SOURCE: _tar_files({
        "pyproject.toml": b'[project]\nname="gp-access-control-plane"\nversion="0.5.0"\n',
        "scripts/install-linux.sh": b"#!/bin/bash\n",
    }),
    WHEEL: wheel_bytes.getvalue(),
    SDIST: _tar_files({"gp_access_control_plane-0.5.0/PKG-INFO": metadata}),
}
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
        cached_wheel = self.state / "strategy-finder" / "downloads" / "0.5.0" / WHEEL
        cached_wheel.write_bytes(b"damaged")
        with patch.object(releases, "fetch_github_releases", return_value=json.dumps([row])) as fetch, patch.object(
            releases, "fetch_release_asset", side_effect=lambda tag, name, size: DELIVERY[name]
        ):
            self.assertEqual(self.call("POST", ROOT + "/download", {"version": "0.5.0"}, token)[0], 200)
            fetch.assert_called_once()
        self.assertEqual(cached_wheel.read_bytes(), DELIVERY[WHEEL])

        cached_wheel.write_bytes(b"damaged")
        incomplete = BytesIO()
        with ZipFile(incomplete, "w") as archive:
            archive.writestr("gp_access_control_plane-0.5.0.dist-info/METADATA", metadata)
        invalid = {**FILES, WHEEL: incomplete.getvalue()}
        sums = "".join(f"{sha256(data).hexdigest()}  {name}\n" for name, data in invalid.items()).encode()
        invalid["SHA256SUMS"] = sums
        row["assets"] = [{"name": name, "size": len(data)} for name, data in invalid.items()]
        with patch.object(releases, "fetch_github_releases", return_value=json.dumps([row])), patch.object(
            releases, "fetch_release_asset", side_effect=lambda tag, name, size: invalid[name]
        ):
            status, _, body = self.call("POST", ROOT + "/download", {"version": "0.5.0"}, token)
        self.assertEqual((status, body["error"]["code"]), (502, "upstream_error"))
        self.assertEqual(cached_wheel.read_bytes(), b"damaged")
