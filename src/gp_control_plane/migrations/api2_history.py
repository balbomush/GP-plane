"""Map legacy run snapshots to the historical subset of API 2."""

import json
import sqlite3
from typing import Any


_TERMINAL = {"completed", "stopped", "failed", "timeout"}
_ACTIVE = {"starting", "running", "stopping"}
_ENGINE_SNAPSHOT_FIELDS = ("stdout_log", "progress_log", "discovery_options")


def _merge_known(target: dict[str, Any], field: str, value: Any, run_id: str) -> None:
    if value is None or value == "":
        return
    if field in target and target[field] != value:
        raise ValueError(f"v11 run {run_id!r} has conflicting {field}")
    target[field] = value


def _read_payload(row: sqlite3.Row, run_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"v11 run {run_id!r} has malformed payload") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"v11 run {run_id!r} has malformed payload")
    for field in ("id", "status", "kind"):
        if payload.get(field) not in (None, "", row[field]):
            raise ValueError(f"v11 run {run_id!r} has conflicting {field}")
    return payload


def _error_info(code: str, message: str) -> str:
    return json.dumps({"code": code, "message": message[:512]}, ensure_ascii=False)


def migrate_history(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM api2_old_runs ORDER BY seq")
    existing = {row[0] for row in conn.execute("SELECT DISTINCT id FROM api2_old_runs WHERE id != ''")}
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        run_id = row["id"]
        if not run_id:
            run_id = f"legacy-{row['seq']}"
            while run_id in existing:
                run_id += "-legacy"
            existing.add(run_id)
        grouped.setdefault(run_id, []).append(row)

    for run_id, snapshots in grouped.items():
        known: dict[str, Any] = {}
        parameters: dict[str, Any] = {}
        finished_at = None
        error = None
        status = ""
        for row in snapshots:
            payload = _read_payload(row, run_id)
            status = row["status"] or status
            interrupted = payload.get("interrupted") is True
            _merge_known(parameters, "kind", row["kind"], run_id)
            _merge_known(parameters, "candidate_id", payload.get("candidate_id"), run_id)
            if not interrupted:
                _merge_known(parameters, "test", payload.get("test"), run_id)
                options = payload.get("discovery_options")
                if isinstance(options, dict):
                    known_options = parameters.setdefault("discovery_options", {})
                    for field, value in options.items():
                        _merge_known(known_options, field, value, run_id)
                if any(field in payload for field in _ENGINE_SNAPSHOT_FIELDS):
                    domains = payload.get("domains")
                    if isinstance(domains, list) and domains and all(isinstance(value, str) for value in domains):
                        _merge_known(known, "domains", domains, run_id)
                    _merge_known(known, "started_at", payload.get("started_at"), run_id)
            if status in _TERMINAL or interrupted:
                finished_at = None if interrupted else payload.get("completed_at") or None
                if interrupted:
                    status = "failed"
                    error = _error_info("interrupted", str(payload.get("interrupted_reason") or "Legacy search interrupted"))
                elif status == "timeout":
                    status = "failed"
                    error = _error_info("timeout", "Legacy search timed out")
                elif status == "failed":
                    error = _error_info("legacy_failure", str(payload.get("error") or "Legacy search failed"))
                else:
                    error = None
        if status in _ACTIVE or not status:
            status = "failed"
            finished_at = None
            error = _error_info("interrupted", "Legacy search has no final result")
        elif status not in _TERMINAL:
            error = _error_info("legacy_failure", f"Legacy search status: {status}")
            status = "failed"
        domains = known.get("domains")
        # Old modes do not establish which executable produced the run.
        conn.execute(
            """INSERT INTO runs(id, milestone_id, parameters_json, domains_json, status, error_json,
                                started_at, finished_at)
               VALUES(?, 0, ?, ?, ?, ?, ?, ?)""",
            (run_id, json.dumps(parameters, ensure_ascii=False) if parameters else None,
             json.dumps(domains, ensure_ascii=False) if domains is not None else None,
             status, error, known.get("started_at"), finished_at),
        )
