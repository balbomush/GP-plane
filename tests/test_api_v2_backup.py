"""User-only backups preserve the destination access and original saved file."""

from io import BytesIO
from pathlib import Path
import unittest
from zipfile import ZipFile

from gp_control_plane import storage
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


ROOT = "/api/v2/backup"
LEGACY = Path(__file__).parent / "fixtures" / "api_v2" / "legacy_backup_v7.zip"


class BackupTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.configured = hasattr(api_v2, "register_backups")
        if self.configured:
            api_v2.register_backups(app, self.state)

    def test_saved_copy_is_user_only_and_upload_does_not_replace_it(self):
        self.assertTrue(self.configured, "backup routes are missing")
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domains(name) VALUES('preserved.example')")
            conn.execute("INSERT INTO milestones VALUES(2)")
        status, _, payload = self.call("POST", ROOT, b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"created_at", "sha256", "size_bytes"})
        status, _, archived = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(len(archived), payload["size_bytes"])
        with ZipFile(BytesIO(archived)) as file:
            all_bytes = b"".join(file.read(name) for name in file.namelist())
            self.assertIn(b"preserved.example", all_bytes)
            self.assertNotIn(b"password_hash", all_bytes)
            self.assertNotIn(b"token_secret", all_bytes)
        status, _, payload = self.call("POST", ROOT + "/restore-file", LEGACY.read_bytes(), token,
                                       "application/octet-stream")
        self.assertEqual((status, payload), (200, {"restored": True}))
        with storage.connect_api2(self.state) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [0, 1])
            self.assertEqual(conn.execute("SELECT count(*) FROM domains WHERE name='preserved.example'").fetchone()[0], 0)
        self.assertEqual(self.call("GET", ROOT + "/file", b"", token)[2], archived)
        self.assertEqual(self.call("POST", ROOT + "/restore", b"", token)[2], {"restored": True})
        with storage.connect_api2(self.state) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [1, 2])
            self.assertEqual(conn.execute("SELECT name FROM domains").fetchone()[0], "preserved.example")
        self.assertEqual(self.call("DELETE", ROOT, b"", token)[0], 204)
        self.assertEqual(self.call("GET", ROOT + "/file", b"", token)[0], 404)
