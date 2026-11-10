"""AUD-01: checksummed but API-incompatible imports must not replace user data."""
from hashlib import sha256
from io import BytesIO
import json
import unittest
from zipfile import ZipFile

from gp_control_plane import storage, strategy_assets, user_backup
from gp_control_plane.http import api_v2
from gp_control_plane.migrations import api2
from tests.support.api2_http import Api2HttpFixture


def edited_archive(archive, table, change):
    # Same mutation/checksum technique as the independent AUD-01 reproducer.
    with ZipFile(BytesIO(archive)) as zipped:
        files = {name: zipped.read(name) for name in zipped.namelist()}
    name = 'snapshot/data/' + table + '.ndjson'
    rows = [json.loads(line) for line in files[name].splitlines()]
    change(rows)
    files[name] = ''.join(json.dumps(row) + '\n' for row in rows).encode()
    checksums = 'snapshot/checksums.sha256'
    files[checksums] = ''.join(
        sha256(data).hexdigest() + '  ' + path.removeprefix('snapshot/') + '\n'
        for path, data in sorted(files.items()) if path != checksums
    ).encode()
    output = BytesIO()
    with ZipFile(output, 'w') as zipped:
        for path, data in files.items():
            zipped.writestr(path, data)
    return output.getvalue()


class BackupSemanticsTests(Api2HttpFixture, unittest.TestCase):
    def configure(self, app):
        api_v2.register_access(app, self.state)
        api_v2.register_backups(app, self.state)
        api_v2.register_domain_lists(app, self.state)
        api_v2.register_catalog(app, self.state)
        api_v2.register_history(app, self.state, resume_available=lambda run, current: False)

    def prepare_archive(self):
        token = self.call('POST', '/api/v2/access/authentication', {
            'username': 'admin', 'password': 'admin',
        })[2]['bearer_token']
        composition = strategy_assets.capture('nfqws2', 'tls', '--lua-init=@test.lua', {'test.lua': b'lua'})
        strategy_assets.persist(self.state / 'strategy-finder/assets', composition)
        with storage.connect_api2(self.state) as conn:
            conn.execute("INSERT INTO domain_presets(id,kind,name) VALUES(1,'user','kept')")
            conn.execute("INSERT INTO milestones(id) VALUES(0)")
            conn.execute("INSERT INTO domains(id,name) VALUES(1,'kept.example')")
            conn.execute("INSERT INTO strategies(id,protocol,args,family) VALUES('kept-strategy','tls','--kept','other')")
            conn.execute("INSERT INTO bundles VALUES(1,?,'kept bundle','[]',?)",
                         (composition.fingerprint, composition.fingerprint))
            conn.execute("INSERT INTO strategy_domain_results VALUES(1,'kept-strategy',1,0)")
            conn.execute("INSERT INTO bundle_domain_results VALUES(1,1,1,0)")
            conn.execute("""INSERT INTO runs(id,milestone_id,status,started_at,finished_at)
                            VALUES('legacy-run',0,'stopped',NULL,NULL)""")
        self.assertEqual(self.call('POST', '/api/v2/backup', b'', token)[0], 200)
        return token, user_backup.saved_path(self.state).read_bytes()

    def snapshot(self):
        with storage.connect_api2(self.state) as conn:
            return {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)]
                    for table in api2.V12_COLUMNS}

    def test_rejected_ids_and_times_preserve_data_access_settings_and_saved_backup(self):
        token, original = self.prepare_archive()
        cases = [('domain_presets', 'id', -1), ('domain_presets', 'id', 0),
                 ('engines', 'id', -1), ('bundles', 'id', -1), ('runs', 'id', ''),
                 ('strategy_domain_results', 'id', -1), ('strategy_domain_results', 'id', 0),
                 ('bundle_domain_results', 'id', -1), ('bundle_domain_results', 'id', 0),
                 ('strategies', 'id', ''),
                 ('runs', 'started_at', 'not-a-date'), ('runs', 'finished_at', '2026-02-30T12:00:00Z'),
                 ('runs', 'started_at', '2026-11-10'), ('runs', 'started_at', '2026-11-10T12:00:00'),
                 ('runs', 'finished_at', '2026-11-10T12:00:00+03:00')]
        for table, field, value in cases:
            bad = edited_archive(original, table, lambda rows: rows[0].update({field: value}))
            if table == 'strategies':
                bad = edited_archive(bad, 'strategy_domain_results', lambda rows: rows[0].update(strategy_id=value))
            for route in ('restore-file', 'restore'):
                with self.subTest(table=table, field=field, value=value, route=route):
                    self.assertEqual(self.call('POST', '/api/v2/backup/restore-file', original,
                                               token, 'application/octet-stream')[0], 200)
                    saved = user_backup.saved_path(self.state)
                    saved.write_bytes(bad if route == 'restore' else original)
                    before = self.snapshot()
                    files = {str(p.relative_to(self.state)): p.read_bytes()
                             for p in self.state.rglob('*') if p.is_file() and p.suffix != '.sqlite3'
                             and not p.name.endswith(('-wal', '-shm'))}
                    status, _, payload = self.call('POST', '/api/v2/backup/' + route,
                        bad if route == 'restore-file' else b'', token, 'application/octet-stream')
                    self.assertEqual(status, 409)
                    self.assertEqual(payload['error']['code'], 'archive_incomplete')
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual({name: (self.state / name).read_bytes() for name in files}, files)
                    self.assertEqual(self.call('GET', '/api/v2/data/domain-lists', b'', token)[0], 200)
                    self.assertEqual(self.call('GET', '/api/v2/data/runs', b'', token)[0], 200)
                    for endpoint in ('strategies', 'bundles'):
                        self.assertEqual(self.call('GET', '/api/v2/data/' + endpoint + '?milestone_id=0', b'', token)[0], 200)
        user_backup.saved_path(self.state).write_bytes(original)

    def test_positive_ids_nullable_history_milestone_zero_and_utc_times_are_preserved(self):
        token, original = self.prepare_archive()
        for started, finished in ((None, None), ('2026-11-10T12:00:00Z', '2026-11-10T12:01:00+00:00'),
                                  ('2024-02-29T12:00:00.123456Z', None)):
            archive = edited_archive(original, 'runs', lambda rows: rows[0].update(
                started_at=started, finished_at=finished))
            for route in ('restore-file', 'restore'):
                with self.subTest(started=started, finished=finished, route=route):
                    user_backup.saved_path(self.state).write_bytes(archive)
                    status, _, payload = self.call('POST', '/api/v2/backup/' + route,
                        archive if route == 'restore-file' else b'', token, 'application/octet-stream')
                    self.assertEqual((status, payload), (200, {'restored': True}))
                    run = self.call('GET', '/api/v2/data/runs/legacy-run', b'', token)[2]
                    self.assertEqual((run['milestone_id'], run['started_at'], run['finished_at']),
                                     (0, started, finished))
                    self.assertIsNone(run['engine'])
                    self.assertIsNone(run['parameters'])
                    for endpoint in ('strategies', 'bundles'):
                        status, _, page = self.call('GET', '/api/v2/data/' + endpoint + '?milestone_id=0', b'', token)
                        self.assertEqual(status, 200)
                        self.assertEqual(page['items'][0]['results'][0]['id'], 1)
