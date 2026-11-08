"""API 2 domain lists and their ordered memberships."""

import json
import sqlite3
import uuid


def view(row: sqlite3.Row) -> dict:
    raw_source = json.loads(row["source_json"])
    source = None
    if isinstance(raw_source, dict) and raw_source.get("type") == "v2fly/domain-list-community":
        categories = raw_source.get("categories")
        source = {
            "code": "v2fly",
            "categories": list(dict.fromkeys(
                category for category in categories if isinstance(category, str) and category
            )) if isinstance(categories, list) else [],
        }
    return {
        "id": row["id"], "name": row["label"] or row["name"],
        "kind": "builtin" if row["kind"] == "system" else "user", "source": source,
    }


def read(conn: sqlite3.Connection, key: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM domain_presets WHERE id=?", (key,)).fetchone()


def page(conn: sqlite3.Connection, kind: str | None, offset: int, limit: int) -> dict:
    where = "" if kind is None else " WHERE kind=?"
    keys = () if kind is None else ("system" if kind == "builtin" else "user",)
    total = conn.execute("SELECT count(*) FROM domain_presets" + where, keys).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM domain_presets" + where + " ORDER BY id LIMIT ? OFFSET ?",
        (*keys, limit, offset),
    )
    return {"items": [view(row) for row in rows], "total": total, "offset": offset, "limit": limit}


def entries(conn: sqlite3.Connection, key: int, offset: int, limit: int) -> dict:
    total = conn.execute("SELECT count(*) FROM preset_domains WHERE preset_id=?", (key,)).fetchone()[0]
    rows = conn.execute(
        """SELECT d.name FROM preset_domains p JOIN domains d ON d.id=p.domain_id
           WHERE p.preset_id=? ORDER BY p.position, d.name LIMIT ? OFFSET ?""",
        (key, limit, offset),
    )
    return {"items": [{"domain": row[0]} for row in rows], "total": total, "offset": offset, "limit": limit}


def write(conn: sqlite3.Connection, key: int | None, name: str, domains: list[str]) -> dict:
    if key is None:
        key = conn.execute(
            """INSERT INTO domain_presets(scope, name, label, kind)
               VALUES('api2', ?, ?, 'user') RETURNING id""",
            (uuid.uuid4().hex, name),
        ).fetchone()[0]
    else:
        conn.execute("UPDATE domain_presets SET label=? WHERE id=?", (name, key))
        conn.execute("DELETE FROM preset_domains WHERE preset_id=?", (key,))
    conn.executemany("INSERT INTO domains(name) VALUES(?) ON CONFLICT(name) DO NOTHING", ((d,) for d in domains))
    conn.executemany(
        """INSERT INTO preset_domains(preset_id, domain_id, position)
           SELECT ?, id, ? FROM domains WHERE name=?""",
        ((key, position, domain) for position, domain in enumerate(domains)),
    )
    return view(read(conn, key))
