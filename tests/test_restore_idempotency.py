"""Transaction and replay edge cases for backup restore."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from gp_control_plane import backups, storage, user_backup
from gp_control_plane.application.operation_admission import OperationAdmission
from gp_control_plane.http.api_v2 import register_access, register_backups
from tests.support.api2_http import Api2HttpFixture
from tests.test_api_v2_backup import LEGACY, ROOT


class RestoreIdempotencyTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        self.admission = OperationAdmission()
        register_access(app, self.state)
        register_backups(app, self.state, admission=self.admission)

    def authorize(self):
        status, _, payload = self.call("POST", "/api/v2/access/authentication", {
            "username": "admin", "password": "admin",
        })
        self.assertEqual(status, 200)
        self.token = payload["bearer_token"]

    def saved_archive(self):
        self.authorize()
        self.add_domain("archived.example")
        self.assertEqual(self.call("POST", ROOT, b"", self.token)[0], 200)
        return user_backup.saved_path(self.state).read_bytes()

    def add_domain(self, name):
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domains(name) VALUES (?)", (name,))

    def domains(self):
        with storage.open_api2(self.state) as conn:
            return {row[0] for row in conn.execute("SELECT name FROM domains")}

    def receipt_count(self):
        with storage.open_api2(self.state) as conn:
            return conn.execute("SELECT count(*) FROM backup_restore_receipts").fetchone()[0]

    def restore(self, key, upload=None):
        headers = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": key}
        route = "/restore"
        if upload is not None:
            route = "/restore-file"
            headers["Content-Type"] = "application/octet-stream"
        conn = HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("POST", ROOT + route, b"" if upload is None else upload, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_receipt_insert_failure_rolls_back_restore(self):
        self.saved_archive()
        self.add_domain("kept-after-backup.example")
        key = str(uuid4())
        with storage.open_api2(self.state) as conn:
            conn.execute("""
                CREATE TRIGGER reject_result BEFORE INSERT ON backup_restore_receipts
                BEGIN SELECT RAISE(ABORT, 'simulated failed receipt'); END
            """)
        self.assertNotEqual(self.restore(key)[0], 200)
        self.assertEqual(self.domains(), {"archived.example", "kept-after-backup.example"})
        self.assertEqual(self.receipt_count(), 0)
        with storage.open_api2(self.state) as conn:
            conn.execute("DROP TRIGGER reject_result")
        self.assertEqual(self.restore(key), (200, {"restored": True}))
        self.assertEqual(self.domains(), {"archived.example"})
        self.assertEqual(self.receipt_count(), 1)

    def test_cleanup_warning_is_replayed_for_identical_file(self):
        content = self.saved_archive()
        key = str(uuid4())
        original_rmtree = user_backup.shutil.rmtree

        def fail_restore_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".user-restore-"):
                raise PermissionError("simulated inaccessible stage")
            return original_rmtree(path, *args, **kwargs)

        with patch.object(user_backup.shutil, "rmtree", side_effect=fail_restore_cleanup):
            first = self.restore(key, content)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[1]["warnings"][0]["code"], "temporary_cleanup_failed")
        self.add_domain("kept-after-restore.example")
        self.assertEqual(self.restore(key, content), first)
        self.assertEqual(self.domains(), {"archived.example", "kept-after-restore.example"})
        self.assertEqual(self.receipt_count(), 1)

    def test_recheck_finds_receipt_after_other_request_commits(self):
        self.saved_archive()
        original_claim = self.admission.claim
        for occupied, marker in ((False, "kept-free.example"), (True, "kept-busy.example")):
            with self.subTest(occupied=occupied):
                first_lookup_finished = Event()
                release = Event()
                key = str(uuid4())

                @contextmanager
                def pause_first_claim(operation):
                    if not first_lookup_finished.is_set():
                        first_lookup_finished.set()
                        if not release.wait(10):
                            raise AssertionError("restore admission was not released")
                    with original_claim(operation):
                        yield

                with patch.object(self.admission, "claim", pause_first_claim), ThreadPoolExecutor() as pool:
                    waiting = pool.submit(self.restore, key)
                    try:
                        self.assertTrue(first_lookup_finished.wait(5))
                        first = self.restore(key)
                        self.assertEqual(first, (200, {"restored": True}))
                        self.add_domain(marker)
                        with original_claim("search") if occupied else nullcontext():
                            release.set()
                            self.assertEqual(waiting.result(10), first)
                    finally:
                        release.set()
                self.assertEqual(self.domains(), {"archived.example", marker})

    def test_legacy_saved_replays_after_copy_is_removed(self):
        self.authorize()
        root = backups.snapshots_dir(self.state)
        root.mkdir(parents=True)
        with ZipFile(LEGACY) as archive:
            archive.extractall(root)
        snapshot, = root.iterdir()
        (root.parent / "latest.txt").write_text(snapshot.name + "\n", encoding="utf-8")
        key = str(uuid4())
        first = self.restore(key)
        self.assertEqual(first, (200, {"restored": True}))
        self.assertEqual(self.receipt_count(), 1)
        self.assertEqual(self.call("DELETE", ROOT, b"", self.token)[0], 204)
        self.assertFalse(user_backup.saved_exists(self.state))
        self.add_domain("kept-after-legacy-restore.example")
        self.assertEqual(self.restore(key), first)
        self.assertIn("kept-after-legacy-restore.example", self.domains())
