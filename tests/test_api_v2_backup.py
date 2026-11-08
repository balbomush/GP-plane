"""User-only backups preserve the destination access and original saved file."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from threading import Event
from unittest.mock import patch
import unittest
from zipfile import ZipFile

from gp_control_plane import storage, user_backup
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

    def test_legacy_saved_copy_is_never_downloaded_raw(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        saved = user_backup.saved_path(self.state)
        saved.parent.mkdir()
        old = LEGACY.read_bytes()
        saved.write_bytes(old)
        status, _, payload = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual((status, payload["error"]["code"]), (409, "archive_incomplete"))
        self.assertEqual(saved.read_bytes(), old)

    def test_checksums_do_not_allow_invalid_history_to_replace_state(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        with storage.connect_api2(self.state) as conn:
            conn.execute(
                """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json)
                   VALUES('kept', 1, 1, 'completed', '{}', '["one.example"]')"""
            )
        self.assertEqual(self.call("POST", ROOT, b"", token)[0], 200)
        saved = user_backup.saved_path(self.state)
        with ZipFile(saved) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        checksums = "snapshot/checksums.sha256"
        del files[checksums]
        runs = "snapshot/data/runs.ndjson"
        for field, invalid in (
            ("status", "broken"), ("domains_json", "invalid"),
            ("parameters_json", '{"test":NaN}'), ("parameters_json", '{"test":1e999}'),
        ):
            with self.subTest(field=field):
                row = json.loads(files[runs])
                original = files[runs]
                row[field] = invalid
                files[runs] = (json.dumps(row) + "\n").encode()
                sums = "".join(
                    f"{sha256(data).hexdigest()}  {name.removeprefix('snapshot/')}\n"
                    for name, data in files.items()
                )
                buffer = BytesIO()
                with ZipFile(buffer, "w") as archive:
                    for name, data in files.items():
                        archive.writestr(name, data)
                    archive.writestr(checksums, sums)
                status, _, payload = self.call("POST", ROOT + "/restore-file", buffer.getvalue(),
                                               token, "application/octet-stream")
                self.assertEqual((status, payload["error"]["code"]), (409, "archive_incomplete"))
                files[runs] = original
        with storage.connect_api2(self.state) as conn:
            self.assertEqual(tuple(conn.execute("SELECT status, domains_json FROM runs").fetchone()),
                             ("completed", '["one.example"]'))

    def test_failed_publication_preserves_saved_copy_and_conflicting_delete_refuses(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        self.assertEqual(self.call("POST", ROOT, b"", token)[0], 200)
        saved = user_backup.saved_path(self.state)
        prior = saved.read_bytes()
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domains(name) VALUES('changed.example')")
        with patch.object(user_backup.backups, "_fsync_directory", side_effect=OSError("sync refused")) as sync:
            self.assertEqual(self.call("POST", ROOT, b"", token)[0], 500)
            sync.assert_called_once_with(saved.parent)
        self.assertEqual(saved.read_bytes(), prior)
        self.assertEqual(self.call("POST", ROOT, b"", token)[0], 200)
        self.assertNotEqual(saved.read_bytes(), prior)
        prior = saved.read_bytes()
        entered, release = Event(), Event()
        real_restore = user_backup.restore

        def hold(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("restore was not released")
            real_restore(*args)

        with patch.object(user_backup, "restore", side_effect=hold), ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(self.call, "POST", ROOT + "/restore", b"", token)
            try:
                self.assertTrue(entered.wait(3))
                status, _, payload = self.call("DELETE", ROOT, b"", token)
                self.assertEqual((status, payload["error"]["code"]), (409, "state_conflict"))
            finally:
                release.set()
            self.assertEqual(request.result(5)[0], 200)
        self.assertEqual(saved.read_bytes(), prior)
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domains(name) VALUES('after-cleanup-error.example')")
        original_rmtree = user_backup.shutil.rmtree

        def fail_stage_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".user-backup-"):
                raise PermissionError("test staged cleanup")
            return original_rmtree(path, *args, **kwargs)

        with patch.object(user_backup.shutil, "rmtree", side_effect=fail_stage_cleanup):
            status, _, metadata = self.call("POST", ROOT, b"", token)
        self.assertEqual(status, 200)
        self.assertEqual(sha256(saved.read_bytes()).hexdigest(), metadata["sha256"])
        self.assertNotEqual(saved.read_bytes(), prior)
