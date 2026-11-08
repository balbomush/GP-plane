"""Catalog selection and projections in a caller-owned read transaction."""

import json
import sqlite3

from ..strategy_safety import FAMILY_ORDER, analyze_strategy


_RANKS = {key: value for key, value in FAMILY_ORDER.items() if key != "other"}
_FAMILY_ORDER_SQL = "CASE entity.family " + " ".join(
    f"WHEN '{family}' THEN {rank}" for family, rank in _RANKS.items()
) + " ELSE 2147483647 END"


def milestone(conn: sqlite3.Connection, requested: int | None) -> int | None:
    if requested is None:
        row = conn.execute("SELECT max(id) FROM milestones").fetchone()
    else:
        row = conn.execute("SELECT id FROM milestones WHERE id=?", (requested,)).fetchone()
    return row[0] if row else None


def _results(conn, kind, key, milestone_id, domains, engine_code):
    table = f"{kind}_domain_results"
    conditions = ["r.milestone_id=?", f"r.{kind}_id=?"]
    args = [milestone_id, key]
    if domains:
        conditions.append("d.name IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(domains))
    if engine_code is not None:
        conditions.append(
            """EXISTS (SELECT 1 FROM strategy_result_engines src
                       JOIN engines e ON e.id=src.engine_id WHERE src.result_id=r.id AND e.code=?)"""
        )
        args.append(engine_code)
    rows = conn.execute(
        f"""SELECT r.id, d.name FROM {table} r JOIN domains d ON d.id=r.domain_id
            WHERE {' AND '.join(conditions)} ORDER BY d.name, r.id""",
        args,
    )
    results = []
    for result_id, domain in rows:
        item = {"id": result_id, "domain": domain}
        if kind == "strategy":
            item["engines"] = [
                dict(row) for row in conn.execute(
                    """SELECT e.id, e.code, e.name FROM strategy_result_engines src
                       JOIN engines e ON e.id=src.engine_id WHERE src.result_id=? ORDER BY e.id""",
                    (result_id,),
                )
            ]
        results.append(item)
    return results


def view(conn, kind, row, milestone_id, *, domains=(), engine_code=None, available):
    item = {
        "id": row["id"], "milestone_id": milestone_id, "archive_available": available(kind, row),
        "results": _results(conn, kind, row["id"], milestone_id, domains, engine_code),
    }
    if kind == "strategy":
        family = row["family"]
        analysis = analyze_strategy(row["protocol"], row["args"])
        item.update(
            protocol=row["protocol"], args=row["args"], family=family, family_rank=_RANKS.get(family),
            family_reason=analysis.family_reason if family in _RANKS and analysis.family == family
            else "Нет описания группы",
        )
    else:
        item.update(name=row["name"], sources=json.loads(row["sources_json"]))
    return item


def read(conn, kind, key):
    table = "strategies" if kind == "strategy" else "bundles"
    return conn.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()


def page(conn, kind, milestone_id, filters, offset, limit, *, available):
    is_strategy = kind == "strategy"
    table = "strategies" if is_strategy else "bundles"
    conditions = []
    args = []
    for key in ("protocol", "family"):
        if filters.get(key) is not None:
            conditions.append(f"entity.{key}=?")
            args.append(filters[key])
    if filters.get("q"):
        conditions.append("instr(lower(entity.args), lower(?)) > 0")
        args.append(filters["q"])
    domains = filters.get("domains", ())
    engine_code = filters.get("engine_code")
    if is_strategy or domains or filters.get("view") != "all":
        result_where = [f"r.{kind}_id=entity.id", "r.milestone_id=?"]
        args.append(milestone_id)
        if domains:
            result_where.append("d.name IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(domains))
        if engine_code is not None:
            result_where.append(
                """EXISTS (SELECT 1 FROM strategy_result_engines src JOIN engines e ON e.id=src.engine_id
                           WHERE src.result_id=r.id AND e.code=?)"""
            )
            args.append(engine_code)
        count = len(domains) if domains and filters.get("match") == "all" else 1
        conditions.append(
            f"""(SELECT count(*) FROM {kind}_domain_results r JOIN domains d ON d.id=r.domain_id
                 WHERE {' AND '.join(result_where)}) >= ?"""
        )
        args.append(count)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = conn.execute(f"SELECT count(*) FROM {table} entity" + where, args).fetchone()[0]
    order = "entity.id"
    if filters.get("sort") == "family":
        order = f"entity.protocol, {_FAMILY_ORDER_SQL}, entity.family, entity.id"
    rows = conn.execute(
        f"SELECT entity.* FROM {table} entity{where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*args, limit, offset),
    )
    return {
        "items": [
            view(conn, kind, row, milestone_id, domains=domains, engine_code=engine_code, available=available)
            for row in rows
        ],
        "total": total, "offset": offset, "limit": limit, "milestone_id": milestone_id,
    }
