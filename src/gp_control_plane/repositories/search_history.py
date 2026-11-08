"""Read one row per API 2 search without exposing its internal checkpoint."""

import json
import sqlite3


ACTIVE = ("starting", "running", "stopping")
TERMINAL = ("completed", "stopped", "failed")
_SELECT = "SELECT r.*, e.code AS engine_code, e.name AS engine_name FROM runs r LEFT JOIN engines e ON e.id=r.engine_id"


def read(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE r.id=?", (run_id,)).fetchone()


def page(conn: sqlite3.Connection, filters: dict, offset: int, limit: int):
    columns = {"status": "r.status", "milestone_id": "r.milestone_id", "engine_code": "e.code"}
    predicates = []
    args = []
    for key, value in filters.items():
        if value is not None:
            predicates.append(columns[key] + "=?")
            args.append(value)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    total = conn.execute(
        "SELECT count(*) FROM runs r LEFT JOIN engines e ON e.id=r.engine_id" + where, args,
    ).fetchone()[0]
    rows = conn.execute(
        _SELECT + where + " ORDER BY r.started_at DESC, r.id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    return rows, total


def _decoded(value):
    return json.loads(value) if value is not None else None


def view(row: sqlite3.Row, *, resumable: bool) -> dict:
    engine = None
    if row["engine_id"] is not None:
        engine = {"id": row["engine_id"], "code": row["engine_code"], "name": row["engine_name"]}
    return {
        "id": row["id"], "engine": engine, "milestone_id": row["milestone_id"],
        "status": row["status"], "error": _decoded(row["error_json"]),
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "domains": _decoded(row["domains_json"]), "domain_list_id": row["preset_id"],
        "parameters": _decoded(row["parameters_json"]), "resume_available": resumable,
    }


def active(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM runs WHERE status IN ('starting','running','stopping') LIMIT 1",
    ).fetchone() is not None
