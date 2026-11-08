"""Atomic v11-to-v12 migration. No old bootstrap or filesystem cutover."""

import json
import re
import sqlite3

from .. import auth
from .api2_history import migrate_history
from .api2_schema import ENGINES, INDEXES, TABLES, UNCHANGED_TABLES


VERSION = 12
NAME = "api2_user_data"
V11_COLUMNS = {
    "app_settings": "key value_json updated_at",
    "domain_presets": "id scope name kind label source_json",
    "domains": "id name service_group",
    "meta": "key value",
    "preset_domains": "preset_id domain_id position enabled",
    "runs": "seq id kind status timestamp payload_json",
    "schema_migrations": "version name applied_at",
    "strategies": (
        "id protocol args args_hash status fragmentation_class fragmentation_safe fragmentation_reason "
        "family family_key family_rank family_reason"
    ),
    "strategy_domain_results": "strategy_id domain_id protocol source_mode",
}
V12_COLUMNS = {
    **{name: V11_COLUMNS[name] for name in ("domain_presets", "domains", "meta", "schema_migrations")},
    "access_state": "id password_hash password_salt token_secret token_version",
    "bundle_domain_results": "id bundle_id domain_id milestone_id",
    "bundles": "id fingerprint name sources_json files_ref",
    "engines": "id code name",
    "milestones": "id",
    "preset_domains": "id preset_id domain_id position",
    "runs": "id engine_id milestone_id parameters_json domains_json preset_id checkpoint_json status error_json started_at finished_at",
    "strategies": "id protocol args fingerprint family assets_ref",
    "strategy_domain_results": "id strategy_id domain_id milestone_id",
    "strategy_result_engines": "id result_id engine_id",
}
REBUILT = ("strategy_domain_results", "preset_domains", "strategies", "runs", "meta", "app_settings")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        if not row[0].startswith("sqlite_")
    }


def _version(conn: sqlite3.Connection, tables: set[str]) -> str:
    if "meta" in tables:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None:
            return row[0]
    return ""


def _validate_layout(conn: sqlite3.Connection, tables: set[str], expected: dict[str, str], version: int) -> None:
    if tables != expected.keys():
        raise ValueError(f"Unexpected v{version} tables")
    for table, columns in expected.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if actual != set(columns.split()):
            raise ValueError(f"Unexpected v{version} columns in {table}")


def _registrations(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    ]


def _legacy_access(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value_json FROM app_settings WHERE key='auth'").fetchone()
    if row is None:
        return auth._initial_settings()
    raw = auth._settings_value(row)
    try:
        settings = auth._validated_settings(raw)
        if not re.fullmatch(r"[0-9a-f]{64}", settings["password_hash"]):
            raise ValueError("invalid hash")
        settings["token_version"] = int(settings["token_version"])
        if settings["token_version"] > 2**63 - 1:
            raise ValueError("invalid version")
        settings["password_salt"].encode("utf-8")
        settings["token_secret"].encode("utf-8")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Invalid v11 auth record") from exc
    return settings


def _store_access(conn: sqlite3.Connection, settings: dict) -> None:
    conn.execute(
        """INSERT INTO access_state(id, password_hash, password_salt, token_secret, token_version)
           VALUES(1, :password_hash, :password_salt, :token_secret, :token_version)""",
        settings,
    )


def _create(conn: sqlite3.Connection) -> None:
    for statement in (*UNCHANGED_TABLES, *TABLES, *INDEXES):
        conn.execute(statement)
    conn.execute("INSERT INTO milestones VALUES(1)")
    conn.executemany("INSERT INTO engines(id, code, name) VALUES(?, ?, ?)", ENGINES)


def _migrate_v11(conn: sqlite3.Connection, tables: set[str], v11_registrations: tuple) -> None:
    _validate_layout(conn, tables, V11_COLUMNS, 11)
    if _registrations(conn) != list(v11_registrations):
        raise ValueError("Unexpected v11 migration registrations")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Invalid v11 foreign keys")
    access = _legacy_access(conn)
    conn.execute("DROP VIEW IF EXISTS domain_stats")
    conn.execute("DROP VIEW IF EXISTS strategy_stats")
    for table in REBUILT:
        conn.execute(f"ALTER TABLE {table} RENAME TO api2_old_{table}")
    _create(conn)
    conn.execute("INSERT INTO milestones VALUES(0)")
    conn.execute("INSERT INTO meta SELECT key, value FROM api2_old_meta")
    conn.execute(
        """INSERT INTO preset_domains(preset_id, domain_id, position)
           SELECT preset_id, domain_id, position FROM api2_old_preset_domains
           ORDER BY preset_id, position, domain_id"""
    )
    protocols: dict[str, str] = {}
    for strategy_id, protocol in conn.execute(
        """SELECT id, protocol FROM api2_old_strategies
           UNION SELECT strategy_id, protocol FROM api2_old_strategy_domain_results"""
    ):
        if protocol:
            if strategy_id in protocols and protocols[strategy_id] != protocol:
                raise ValueError(f"Conflicting v11 protocols for strategy {strategy_id!r}")
            protocols[strategy_id] = protocol
    rows = conn.execute("SELECT id, args, family FROM api2_old_strategies ORDER BY id")
    conn.executemany(
        "INSERT INTO strategies(id, protocol, args, family) VALUES(?, ?, ?, ?)",
        ((row[0], protocols.get(row[0], ""), row[1], row[2]) for row in rows),
    )
    # v11 source_mode does not establish a producing engine or execution format.
    conn.execute(
        """INSERT INTO strategy_domain_results(strategy_id, domain_id, milestone_id)
           SELECT DISTINCT strategy_id, domain_id, 0 FROM api2_old_strategy_domain_results
           ORDER BY strategy_id, domain_id"""
    )
    migrate_history(conn)
    _store_access(conn, access)
    for table in REBUILT:
        conn.execute(f"DROP TABLE api2_old_{table}")


def _validate_target(conn: sqlite3.Connection, tables: set[str], v11_registrations: tuple) -> None:
    _validate_layout(conn, tables, V12_COLUMNS, VERSION)
    if _registrations(conn) not in ([(VERSION, NAME)], [*v11_registrations, (VERSION, NAME)]):
        raise ValueError("Unexpected v12 migration registrations")

def initialize(conn: sqlite3.Connection, v11_registrations: tuple, *, legacy_idle) -> None:
    """Initialize once under a writer lock; existing target DBs use a reader snapshot."""
    conn.execute("BEGIN")
    try:
        tables = _tables(conn)
        if _version(conn, tables) == str(VERSION):
            _validate_target(conn, tables, v11_registrations)
            conn.commit()
            return
        conn.rollback()
    except BaseException:
        conn.rollback()
        raise

    conn.execute("BEGIN IMMEDIATE")
    try:
        tables = _tables(conn)
        version = _version(conn, tables)
        if version == str(VERSION):
            _validate_target(conn, tables, v11_registrations)
            conn.commit()
            return
        if not tables:
            _create(conn)
            _store_access(conn, auth._initial_settings())
        elif version == "11":
            if not legacy_idle():
                raise ValueError("Cannot migrate while a legacy search is active")
            _migrate_v11(conn, tables, v11_registrations)
        else:
            raise ValueError("Unsupported input schema for API 2 migration")
        conn.execute(
            """INSERT INTO meta(key, value) VALUES('schema_version', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(VERSION),),
        )
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES(?, ?)", (VERSION, NAME))
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("API 2 migration produced invalid foreign keys")
        if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise ValueError('API 2 migration failed integrity check')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
