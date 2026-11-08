"""User-only backups preserve the destination access and original saved file."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
from http.client import HTTPConnection
from io import BytesIO
import json
from socket import SHUT_WR
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
import unittest
from zipfile import ZipFile

from gp_control_plane import storage, strategy_assets, user_backup
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http import api_v2
from tests.support.api2_http import Api2HttpFixture


ROOT = "/api/v2/backup"
LEGACY = Path(__file__).parent / "fixtures" / "api_v2" / "legacy_backup_v7.zip"


def wait_for_download_cleanup(fixture, token):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        status, _, payload = fixture.call("GET", "/api/v2/service/status", b"", token)
        fixture.assertEqual(status, 200)
        if not payload["busy"]:
            return
        fixture.assertEqual(payload["operation"], "archive_download")
        sleep(0.02)
    fixture.fail("backup download did not release its slot")


class BackupTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        self.configured = hasattr(api_v2, "register_backups")
        if self.configured:
            admission = OperationAdmission()
            api_v2.register_backups(app, self.state, admission=admission)
            api_v2.register_service(app, self.state, admission=admission)

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
        wait_for_download_cleanup(self, token)
        status, _, payload = self.call("POST", ROOT + "/restore-file", LEGACY.read_bytes(), token,
                                       "application/octet-stream")
        self.assertEqual((status, payload), (200, {"restored": True}))
        with storage.connect_api2(self.state) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [0, 1])
            self.assertEqual(conn.execute("SELECT count(*) FROM domains WHERE name='preserved.example'").fetchone()[0], 0)
        self.assertEqual(self.call("GET", ROOT + "/file", b"", token)[2], archived)
        wait_for_download_cleanup(self, token)
        self.assertEqual(self.call("POST", ROOT + "/restore", b"", token)[2], {"restored": True})
        with storage.connect_api2(self.state) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [1, 2])
            self.assertEqual(conn.execute("SELECT name FROM domains").fetchone()[0], "preserved.example")
        self.assertEqual(self.call("DELETE", ROOT, b"", token)[0], 204)
        self.assertEqual(self.call("GET", ROOT + "/file", b"", token)[0], 404)

    def test_round_trip_restores_user_tables_and_files_into_a_fresh_installation(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        bundle = strategy_assets.capture("nfqws2", "tls", "--lua-init=@test.lua", {"test.lua": b"lua"})
        strategy_assets.persist(self.state / "strategy-finder" / "assets", bundle)
        with storage.connect_api2(self.state) as conn:
            conn.executescript("""
                INSERT INTO domains(id, name) VALUES(1, 'backup.example');
                INSERT INTO domain_presets(id, kind, name) VALUES(1, 'user', 'backup list');
                INSERT INTO preset_domains(preset_id, domain_id, position) VALUES(1, 1, 0);
                INSERT INTO runs(id, milestone_id, engine_id, status, preset_id, parameters_json, domains_json)
                VALUES('kept', 1, 2, 'running', 1, '{}', '["backup.example"]');
            """)
            conn.execute("INSERT INTO bundles VALUES(1, ?, 'unconfirmed', '[]', ?)",
                         (bundle.fingerprint, bundle.fingerprint))
        storage.save_strategy_check(self.state, run_id="kept", protocol="tls", execution_format="nfqws2",
                                    args="--blob=hello:@hello.bin", files={"hello.bin": b"data"},
                                    outcomes={"backup.example": True}, checkpoint={"revision": "fixture", "trail": []})
        with storage.connect_api2(self.state) as conn:
            conn.execute("UPDATE runs SET status='stopped' WHERE id='kept'")
            conn.execute("INSERT INTO milestones VALUES(2)")
            original = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                        for table in user_backup.USER_TABLES}
        self.assertEqual(self.call("POST", ROOT, b"", token)[0], 200)
        status, _, archive = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual(status, 200)
        wait_for_download_cleanup(self, token)
        with TemporaryDirectory() as temp:
            target, stage = Path(temp) / "fresh", Path(temp) / "stage"
            stage.mkdir()
            with storage.connect_api2(target) as conn:
                conn.execute("UPDATE access_state SET token_version=123 WHERE id=1")
                access = tuple(conn.execute("SELECT * FROM access_state").fetchone())
            user_backup.restore(target, BytesIO(archive), stage)
            with storage.connect_api2(target) as conn:
                restored = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                            for table in user_backup.USER_TABLES}
                self.assertEqual(restored, original)
                self.assertEqual(tuple(conn.execute("SELECT * FROM access_state").fetchone()), access)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            path = Path("strategy-finder") / "assets"
            self.assertEqual({file.relative_to(self.state / path): file.read_bytes()
                              for file in (self.state / path).rglob("*") if file.is_file()},
                             {file.relative_to(target / path): file.read_bytes()
                              for file in (target / path).rglob("*") if file.is_file()})

    def test_restore_upload_owns_admission_before_body_and_refuses_truncated_input(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domains(name) VALUES('untouched.example')")
        with closing(HTTPConnection("127.0.0.1", self.port, timeout=5)) as connection:
            connection.putrequest("POST", ROOT + "/restore-file")
            connection.putheader("Authorization", "Bearer " + token)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", "32")
            connection.endheaders(b"broken")
            deadline = monotonic() + 3
            while monotonic() < deadline:
                _, _, status = self.call("GET", "/api/v2/service/status", b"", token)
                if status["operation"] == "backup_restore":
                    break
                sleep(0.02)
            else:
                self.fail("restore did not claim the slot while waiting for bytes")
            status, _, body = self.call("POST", ROOT, b"", token)
            self.assertEqual((status, body["error"]["code"]), (409, "busy"))
            connection.sock.shutdown(SHUT_WR)
            with closing(connection.getresponse()) as response:
                self.assertEqual(response.status, 400)
                self.assertEqual(json.load(response)["error"]["code"], "invalid_request")
        with storage.connect_api2(self.state) as conn:
            self.assertEqual(conn.execute("SELECT name FROM domains").fetchone()[0], "untouched.example")
        self.assertFalse(list(user_backup.saved_path(self.state).parent.glob(".user-restore-*")))

    def test_legacy_saved_copy_is_never_downloaded_raw(self):
        token = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })[2]["bearer_token"]
        saved = user_backup.saved_path(self.state)
        saved.parent.mkdir()
        old = LEGACY.read_bytes()
        saved.write_bytes(old)
        status, _, payload = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual(status, 200)
        self.assertNotEqual(payload, old)
        with ZipFile(BytesIO(payload)) as converted:
            self.assertTrue(all("/settings/" not in name for name in converted.namelist()))
        self.assertEqual(saved.read_bytes(), old)
        wait_for_download_cleanup(self, token)
        saved.write_bytes(b"incomplete")
        status, _, payload = self.call("GET", ROOT + "/file", b"", token)
        self.assertEqual((status, payload["error"]["code"]), (409, "archive_incomplete"))

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
