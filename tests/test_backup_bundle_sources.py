"""AUD3-01: an optional source name must survive an unchanged own-backup round trip."""
from hashlib import sha256
import json
import unittest

from gp_control_plane import storage, user_backup
from gp_control_plane.http.api_v2.app import create_api2_app
from gp_control_plane.http.asgi import CoreHttpServer
from tests import test_backup_semantics as semantics
from tests.support.api2_http import Api2HttpFixture
from tests.test_api_v2_backup import wait_for_download_cleanup


class BundleSourceBackupTests(Api2HttpFixture, unittest.TestCase):
    prepare_archive = semantics.BackupSemanticsTests.prepare_archive
    snapshot = semantics.BackupSemanticsTests.snapshot

    def configure(self, app):
        pass

    def create_runtime(self, app):
        return CoreHttpServer(create_api2_app(self.state), '127.0.0.1', self.port)

    def own_archive(self, token):
        status, _, created = self.call('POST', '/api/v2/backup', b'', token)
        self.assertEqual(status, 200)
        status, _, archive = self.call('GET', '/api/v2/backup/file', b'', token)
        self.assertEqual(status, 200)
        wait_for_download_cleanup(self, token)
        self.assertEqual(sha256(archive).hexdigest(), created['sha256'])
        self.assertEqual(len(archive), created['size_bytes'])
        self.assertEqual(user_backup.saved_path(self.state).read_bytes(), archive)
        return archive

    def test_own_backup_round_trips_string_null_and_missing_names_without_replacing_access_or_saved_copy(self):
        token, _ = self.prepare_archive()
        cases = [{'source': 'synthetic', 'name': 'Имя источника'},
                 {'source': 'synthetic', 'name': None}, {'source': 'synthetic'}]
        for index, source in enumerate(cases):
            with storage.connect_api2(self.state) as conn:
                conn.execute('UPDATE bundles SET sources_json=? WHERE id=1', (json.dumps([source]),))
            original = self.own_archive(token)
            expected_user_rows = {t: rows for t, rows in self.snapshot().items() if t in user_backup.USER_TABLES}
            if index == 0:
                status, _, _ = self.call('PUT', '/api/v2/access/change_password', {
                    'username': 'admin', 'old_password': 'admin', 'new_password': 'after-backup-secret',
                }, token)
                self.assertEqual(status, 204)
                token = self.call('POST', '/api/v2/access/authentication', {
                    'username': 'admin', 'password': 'after-backup-secret',
                })[2]['bearer_token']
            for route in ('restore-file', 'restore'):
                with self.subTest(source=source, route=route):
                    # Prove restoration replaces user content, rather than succeeding as a no-op.
                    with storage.connect_api2(self.state) as conn:
                        conn.execute('UPDATE bundles SET sources_json=? WHERE id=1',
                                     (json.dumps([{'source': 'changed', 'name': 'after backup'}]),))
                    before = self.snapshot()
                    status, _, payload = self.call('POST', '/api/v2/backup/' + route,
                        original if route == 'restore-file' else b'', token, 'application/octet-stream')
                    self.assertEqual((status, payload), (200, {'restored': True}))
                    after = self.snapshot()
                    self.assertEqual({t: rows for t, rows in after.items() if t in user_backup.USER_TABLES}, expected_user_rows)
                    self.assertEqual(after['access_state'], before['access_state'])
                    self.assertEqual(user_backup.saved_path(self.state).read_bytes(), original)
                    with storage.connect_api2(self.state) as conn:
                        self.assertEqual(json.loads(conn.execute('SELECT sources_json FROM bundles WHERE id=1').fetchone()[0]), [source])
                    status, _, bundle = self.call('GET', '/api/v2/data/bundles/1?milestone_id=0', b'', token)
                    self.assertEqual(status, 200)
                    self.assertEqual(bundle['sources'][0].get('name'), source.get('name'))

    def test_wrong_name_types_are_rejected_before_replacing_data_access_or_saved_backup(self):
        token, _ = self.prepare_archive()
        with storage.connect_api2(self.state) as conn:
            conn.execute('UPDATE bundles SET sources_json=? WHERE id=1',
                         (json.dumps([{'source': 'synthetic', 'name': 'valid'}]),))
        original = self.own_archive(token)
        saved = user_backup.saved_path(self.state)
        for wrong in (0, True, 1.5, [], {}):
            bad = semantics.edited_archive(original, 'bundles', lambda rows: rows[0].update(
                sources_json=json.dumps([{'source': 'synthetic', 'name': wrong}])))
            for route in ('restore-file', 'restore'):
                with self.subTest(wrong=wrong, route=route):
                    saved.write_bytes(bad if route == 'restore' else original)
                    expected_saved = saved.read_bytes()
                    before = self.snapshot()
                    status, _, response = self.call('POST', '/api/v2/backup/' + route,
                        bad if route == 'restore-file' else b'', token, 'application/octet-stream')
                    self.assertEqual(status, 409)
                    self.assertEqual(response['error']['code'], 'archive_incomplete')
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(saved.read_bytes(), expected_saved)
                    for endpoint in ('domain-lists', 'runs', 'bundles', 'strategies'):
                        self.assertEqual(self.call('GET', '/api/v2/data/' + endpoint, b'', token)[0], 200)
