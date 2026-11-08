"""The installed v7 snapshot layout remains usable without exposing secrets."""

from zipfile import ZipFile
import unittest

from gp_control_plane import backups
from gp_control_plane.http.api_v2 import register_access, register_backups
from tests.support.api2_http import Api2HttpFixture
from tests.test_api_v2_backup import LEGACY, ROOT


class LegacySavedBackupTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        register_access(app, self.state)
        register_backups(app, self.state)

    def test_legacy_saved_snapshot_restores_without_serving_raw_bytes(self):
        root = backups.snapshots_dir(self.state)
        root.mkdir(parents=True)
        with ZipFile(LEGACY) as archive:
            archive.extractall(root)
        snapshot, = root.iterdir()
        marker = root.parent / "latest.txt"
        marker.write_text(snapshot.name + "\n", encoding="utf-8")
        _, _, auth = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })
        token = auth["bearer_token"]
        status, _, error = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual((status, error["error"]["code"]), (409, "archive_incomplete"))
        status, _, restored = self.call("POST", ROOT + "/restore", b"", token)
        self.assertEqual((status, restored), (200, {"restored": True}))
        self.assertEqual(self.call("DELETE", ROOT, b"", token)[0], 204)
        self.assertEqual(self.call("GET", ROOT + "/file", b"", token)[0], 404)
        self.assertTrue(snapshot.is_dir())
