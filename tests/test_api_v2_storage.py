"""A complete strategy has one identity and is stored with one checkpoint."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gp_control_plane import storage


class StrategyPersistenceTests(unittest.TestCase):
    def test_complete_strategy_and_checkpoint_commit_together(self):
        save = getattr(storage, "save_strategy_check", None)
        self.assertIsNotNone(save, "strategy result writer is missing")
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with storage.connect_api2(state) as conn:
                conn.execute(
                    """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json)
                       VALUES('gp', 1, 2, 'running', '{}', '["one.example","two.example"]')"""
                )
                conn.execute(
                    """INSERT INTO runs(id, milestone_id, engine_id, status, parameters_json, domains_json)
                       VALUES('stock', 1, 1, 'running', '{}', '["one.example","two.example"]')"""
                )
                conn.executescript("""
                    CREATE TRIGGER reject_checkpoint BEFORE UPDATE OF checkpoint_json ON runs
                    WHEN NEW.checkpoint_json = '{"index":3}'
                    BEGIN SELECT RAISE(ABORT, 'checkpoint rejected'); END;
                """)

            def record(blob, path, checkpoint, run_id="gp", outcomes=None):
                if outcomes is None:
                    outcomes = {"one.example": True, "two.example": False}
                return save(
                    state, run_id=run_id, protocol="tls", execution_format="nfqws2",
                    args=f"--filter-tcp=443 --blob=tls:@'{path}' --lua-desync=fake:blob=tls",
                    files={path: blob}, outcomes=outcomes, checkpoint=checkpoint,
                )

            with self.assertRaisesRegex(ValueError, "all run domains"):
                record(b"first", "/first/a.bin", {"index": 1}, outcomes={"one.example": True})
            with storage.connect_api2(state) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT checkpoint_json FROM runs WHERE id='gp'").fetchone()[0])

            first = record(b"first", "/first/a.bin", {"index": 1})
            again = record(b"first", "/other/b.bin", None, run_id="stock")
            self.assertEqual(first, again)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "checkpoint rejected"):
                record(b"third", "/other/b.bin", {"index": 3})
            with storage.connect_api2(state) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_domain_results").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_result_engines").fetchone()[0], 2)
                self.assertEqual(json.loads(conn.execute(
                    "SELECT checkpoint_json FROM runs WHERE id='gp'"
                ).fetchone()[0]), {"index": 1})
            second = record(b"second", "/other/b.bin", {"index": 4})
            self.assertNotEqual(first, second)
            self.assertIsNone(record(b"fourth", "/other/b.bin", {"index": 5},
                                     outcomes={"one.example": False, "two.example": False}))
            with storage.connect_api2(state) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_domain_results").fetchone()[0], 2)
                self.assertEqual(json.loads(conn.execute(
                    "SELECT checkpoint_json FROM runs WHERE id='gp'"
                ).fetchone()[0]), {"index": 5})
                strategy = conn.execute("SELECT args, assets_ref FROM strategies WHERE id=?", (first,)).fetchone()
                self.assertNotIn("/first", strategy["args"])
                asset = state / "strategy-finder" / "assets" / strategy["assets_ref"] / "assets" / "1.bin"
                self.assertEqual(asset.read_bytes(), b"first")
