"""Write one completed strategy check and its engine position together."""

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
import uuid

from .. import strategy_assets
from ..strategy_safety import analyze_strategy


def save_check(
    conn: sqlite3.Connection,
    asset_root: Path,
    *,
    run_id: str,
    protocol: str,
    execution_format: str,
    args: str,
    files: Mapping[str, bytes],
    outcomes: Mapping[str, bool],
    checkpoint: dict | None,
) -> str | None:
    run = conn.execute(
        """SELECT r.status, r.milestone_id, r.engine_id, r.domains_json, e.code
           FROM runs r JOIN engines e ON e.id=r.engine_id WHERE r.id=?""",
        (run_id,),
    ).fetchone()
    current = conn.execute("SELECT max(id) FROM milestones").fetchone()[0]
    if (run is None or run["status"] not in ("running", "stopping")
            or not run["milestone_id"] or run["milestone_id"] != current):
        raise ValueError("Run must be active in the current milestone")
    if set(json.loads(run["domains_json"])) != outcomes.keys() or not all(
        type(outcome) is bool for outcome in outcomes.values()
    ):
        raise ValueError("Strategy must be checked for all run domains before saving")
    if run["code"] == "blockcheck2-gp":
        if not isinstance(checkpoint, dict) or not checkpoint:
            raise ValueError("GP strategy check requires a checkpoint")
    elif checkpoint is not None:
        raise ValueError("This engine does not store a checkpoint")

    strategy_id = None
    positive = [domain for domain, passed in outcomes.items() if passed]
    if positive:
        composition = strategy_assets.capture(execution_format, protocol, args, files)
        assets_ref = strategy_assets.persist(asset_root, composition)
        existing = conn.execute(
            "SELECT id, args, protocol, assets_ref FROM strategies WHERE fingerprint=?",
            (composition.fingerprint,),
        ).fetchone()
        if existing is not None:
            strategy_id = existing["id"]
            if (existing["args"], existing["protocol"], existing["assets_ref"]) != (
                composition.args, protocol, assets_ref,
            ):
                raise ValueError("Stored strategy definition differs from the captured composition")
        else:
            strategy_id = uuid.uuid4().hex
            family = analyze_strategy(protocol, composition.args).family
            conn.execute(
                """INSERT INTO strategies(id, protocol, args, fingerprint, family, assets_ref)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (strategy_id, protocol, composition.args, composition.fingerprint, family, assets_ref),
            )
        for domain in positive:
            conn.execute("INSERT INTO domains(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (domain,))
            domain_id = conn.execute("SELECT id FROM domains WHERE name=?", (domain,)).fetchone()[0]
            key = (strategy_id, domain_id, current)
            conn.execute(
                """INSERT INTO strategy_domain_results(strategy_id, domain_id, milestone_id)
                   VALUES(?, ?, ?) ON CONFLICT(strategy_id, domain_id, milestone_id) DO NOTHING""",
                key,
            )
            result_id = conn.execute(
                """SELECT id FROM strategy_domain_results
                   WHERE strategy_id=? AND domain_id=? AND milestone_id=?""",
                key,
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO strategy_result_engines(result_id, engine_id) VALUES(?, ?)
                   ON CONFLICT(result_id, engine_id) DO NOTHING""",
                (result_id, run["engine_id"]),
            )
    if checkpoint is not None:
        encoded = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        conn.execute("UPDATE runs SET checkpoint_json=? WHERE id=?", (encoded, run_id))
    return strategy_id
