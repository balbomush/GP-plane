"""Generate the migration fixtures using the unmodified Core 0.5.0 source."""

import argparse
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from gp_control_plane import auth, backups, storage


def generate(target: Path) -> None:
    with TemporaryDirectory() as temp:
        state = Path(temp)
        date = "2026-10-28T09:00:00Z"
        with storage.connect(state) as conn:
            conn.executemany("INSERT INTO domains(id, name) VALUES(?, ?)", [
                (3, "one.example"), (7, "two.example"),
            ])
            conn.executemany(
                "INSERT INTO domain_presets(id, scope, name, kind) VALUES(?, ?, ?, ?)",
                [(5, "custom", "sample", "user"), (6, "system", "sample", "system")],
            )
            conn.executemany("INSERT INTO preset_domains VALUES(?, ?, ?, ?)", [
                (5, 3, -2, 0), (5, 7, 0, 1), (6, 7, 0, 1),
            ])
            conn.executemany(
                "INSERT INTO strategies(id, protocol, args, family) VALUES(?, ?, ?, ?)",
                [
                    ("unverified", "tls", "--lua-desync=fake:blob=unknown", "fake"),
                    ("same-text", "tls", "--lua-desync=fake:blob=unknown", "fake"),
                    ("unknown-protocol", "", "--unknown", "other"),
                ],
            )
            conn.executemany("INSERT INTO strategy_domain_results VALUES(?, ?, ?, ?)", [
                ("unverified", 3, "tls", "single_domain"),
                ("unverified", 3, "tls", "multi_domain"),
                ("same-text", 7, "tls", "single_domain"),
                ("unknown-protocol", 7, "", "single_domain"),
            ])
            conn.execute("UPDATE schema_migrations SET applied_at = ?", (date,))
        auth.login(state, {"username": "admin", "password": "admin"})
        auth.change_password(state, {
            "current_password": "admin", "new_password": "migration-fixture-only",
        })
        storage.save_app_setting(state, "run_settings", {"parallel": 9}, date)
        runs = [
            {"id": "kept", "kind": "discover", "test": "standard", "status": "running",
             "started_at": date, "domains": ["one.example"], "discovery_options": {"parallel": 2}},
            {"id": "kept", "kind": "discover", "test": "standard", "status": "failed",
             "timestamp": "2026-10-28T09:02:00Z", "interrupted": True,
             "interrupted_reason": "service stopped", "domains": []},
            {"id": "", "kind": "old-kind", "status": "timeout", "candidate_id": "unverified"},
            {"id": "", "kind": "old-kind", "status": "stopped"},
        ]
        for run in runs:
            storage.append_run(state, run)
        with storage.connect(state) as conn:
            dump = "\n".join(conn.iterdump())
        (target / "legacy_v11.sql").write_text(dump + "\n", encoding="utf-8", newline="\n")
        snapshot = backups.create_snapshot(state)
        archive = backups.snapshot_archive_path(state, snapshot["snapshot"]["id"])
        (target / "legacy_backup_v7.zip").write_bytes(archive.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    generate(parser.parse_args().target)
