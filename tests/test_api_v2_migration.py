"""Storage cutover tests for fresh and genuine Core 0.5.0 databases."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gp_control_plane import auth, storage


FIXTURES = Path(__file__).parent / "fixtures" / "api_v2"
TABLES = {
    "access_state", "bundle_domain_results", "bundles", "domain_presets",
    "domains", "engines", "meta", "milestones", "preset_domains", "runs",
    "schema_migrations", "strategies", "strategy_domain_results",
    "strategy_result_engines",
}


class Api2MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def load_legacy(self):
        path = storage.db_path(self.state)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript((FIXTURES / "legacy_v11.sql").read_text(encoding="utf-8"))
        return path

    def open_target(self):
        return storage.connect_api2(self.state)

    def test_fresh_db_has_target_constraints_and_access(self):
        with self.open_target() as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
                if not row[0].startswith("sqlite_")
            }
            self.assertEqual(tables, TABLES)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [1])
            access = dict(conn.execute("SELECT * FROM access_state").fetchone())
            self.assertTrue(auth._password_matches(access, "admin"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO strategies VALUES(NULL, '', '', NULL, 'other', NULL)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO runs(id, milestone_id, status) VALUES('bad', 1, 'starting')")

    def test_v11_preserves_user_meaning_and_old_token(self):
        self.load_legacy()
        token = auth.login(self.state, {"username": "admin", "password": "migration-fixture-only"})
        with self.open_target() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM milestones")], [0, 1])
            access = dict(conn.execute("SELECT * FROM access_state").fetchone())
            auth._validate_bearer_token(access, "Bearer " + token["access_token"])
            self.assertEqual(access["token_version"], 2)
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT preset_id, domain_id, position FROM preset_domains ORDER BY preset_id, position"
                )],
                [(5, 3, -2), (5, 7, 0), (6, 7, 0)],
            )
            self.assertEqual(
                [tuple(row) for row in conn.execute("SELECT id, protocol, fingerprint FROM strategies ORDER BY id")],
                [("same-text", "tls", None), ("unknown-protocol", "", None), ("unverified", "tls", None)],
            )
            self.assertEqual(
                [tuple(row) for row in conn.execute(
                    "SELECT strategy_id, domain_id, milestone_id FROM strategy_domain_results ORDER BY strategy_id"
                )],
                [("same-text", 7, 0), ("unknown-protocol", 7, 0), ("unverified", 3, 0)],
            )
            run = dict(conn.execute("SELECT * FROM runs WHERE id='kept'").fetchone())
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["started_at"], "2026-10-28T09:00:00Z")
            self.assertIsNone(run["finished_at"])
            self.assertIsNone(run["checkpoint_json"])
            self.assertIsNone(run["preset_id"])
            self.assertEqual(json.loads(run["domains_json"]), ["one.example"])
            self.assertEqual(json.loads(run["parameters_json"])["discovery_options"], {"parallel": 2})
            missing_id_runs = conn.execute("SELECT * FROM runs WHERE id != 'kept' ORDER BY status").fetchall()
            self.assertEqual(len(missing_id_runs), 2)
            timeout, stopped = missing_id_runs
            self.assertEqual(json.loads(timeout["error_json"])["code"], "timeout")
            self.assertEqual(stopped["status"], "stopped")
            self.assertIsNone(timeout["engine_id"])
            self.assertIsNone(timeout["domains_json"])
            self.assertEqual(json.loads(timeout["parameters_json"])["candidate_id"], "unverified")
            self.assertEqual(conn.execute("SELECT applied_at FROM schema_migrations WHERE version=11").fetchone()[0],
                             "2026-10-28T09:00:00Z")
        with self.open_target() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=12").fetchone()[0], 1)

    def test_bad_auth_and_protocol_conflict_preserve_the_original_db(self):
        for mutation, error in [
            ("UPDATE app_settings SET value_json='null' WHERE key='auth'", "auth"),
            ("UPDATE strategy_domain_results SET protocol='quic' WHERE strategy_id='unverified'", "unverified"),
            ("UPDATE schema_migrations SET name='forged' WHERE version=11", "v11"),
        ]:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as state:
                self.state = Path(state)
                path = self.load_legacy()
                with closing(sqlite3.connect(path)) as conn:
                    conn.execute(mutation)
                    conn.commit()
                    before = list(conn.iterdump())
                with self.assertRaisesRegex(ValueError, error):
                    self.open_target()
                with closing(sqlite3.connect(path)) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def test_legacy_engine_facts_and_job_terminal_snapshot_are_distinct(self):
        self.load_legacy()
        created = "2026-10-28T09:10:00Z"
        started = "2026-10-28T09:10:02Z"
        finished = "2026-10-28T09:11:01Z"
        for run_id, terminal in (("complete", "success"), ("cancel", "cancelled"), ("failure", "failed")):
            storage.append_run(self.state, {"id": run_id, "kind": "zapret-standard-discovery",
                                           "status": "queued", "started_at": created})
            storage.append_run(self.state, {
                "id": run_id, "kind": "standard-discovery", "status": "running",
                "started_at": started, "domains": ["one.example"], "stdout_log": "engine.log",
            })
            job_failure = terminal == "failed"
            storage.append_run(self.state, {
                "id": run_id, "kind": "zapret-standard-discovery" if job_failure else "standard-discovery",
                "status": terminal, "started_at": created if job_failure else started,
                "completed_at": finished, "stdout_log": "engine.log",
            })
        with self.open_target() as conn:
            for run_id, expected in (("complete", "completed"), ("cancel", "stopped"), ("failure", "failed")):
                row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                self.assertEqual((row["status"], row["started_at"], row["finished_at"]), (expected, started, finished))

    def test_migration_rejects_missing_retained_unique_constraint(self):
        fixture = (FIXTURES / "legacy_v11.sql").read_text(encoding="utf-8")
        changed = fixture.replace("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL", 1)
        self.assertNotEqual(fixture, changed)
        path = storage.db_path(self.state)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(changed)
        with self.assertRaisesRegex(ValueError, "unique key in domains"):
            self.open_target()
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "11")

    def test_migration_rejects_int_primary_key_without_rowid_alias(self):
        fixture = (FIXTURES / "legacy_v11.sql").read_text(encoding="utf-8")
        changed = fixture.replace(
            "CREATE TABLE domains (\n            id INTEGER PRIMARY KEY AUTOINCREMENT",
            "CREATE TABLE domains (\n            id INT PRIMARY KEY",
        )
        self.assertNotEqual(fixture, changed)
        with closing(sqlite3.connect(storage.db_path(self.state))) as conn:
            conn.executescript(changed)
        with self.assertRaisesRegex(ValueError, "rowid primary key in domains"):
            self.open_target()

    def test_rejects_layout_that_can_reuse_deleted_list_ids(self):
        fixture = (FIXTURES / "legacy_v11.sql").read_text(encoding="utf-8")
        changed = fixture.replace(
            "CREATE TABLE domain_presets (\n            id INTEGER PRIMARY KEY AUTOINCREMENT",
            "CREATE TABLE domain_presets (\n            id INTEGER PRIMARY KEY",
        )
        self.assertNotEqual(fixture, changed)
        with closing(sqlite3.connect(storage.db_path(self.state))) as conn:
            conn.executescript(changed)
        with self.assertRaisesRegex(ValueError, "ID allocation in domain_presets"):
            self.open_target()

    def test_registration_failure_rolls_back_schema_and_rows(self):
        path = self.load_legacy()
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                CREATE TRIGGER reject_cutover BEFORE INSERT ON schema_migrations
                WHEN NEW.version=12 BEGIN SELECT RAISE(ABORT, 'cutover rejected'); END;
            """)
            before = list(conn.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cutover rejected"):
            self.open_target()
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)
