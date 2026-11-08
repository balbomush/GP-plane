from __future__ import annotations

from .repositories import runs as _runs_repository, candidates as _candidates_repository, presets as _presets_repository
from .repositories.primitives import _args_hash, _upsert_strategy_conn, _upsert_domain_conn, _save_domain_preset_conn, _ensure_system_domain_presets_conn, _unique_nonempty, SYSTEM_DOMAIN_PRESETS, SYSTEM_DOMAIN_PRESET_NAMES
from .repositories.run_payload import compact_run_payload, _compact_payload_value
from .repositories.runs import _page_int
from .repositories.runs import _decode_run_payload_rows
from .repositories.candidates import _upsert_candidate_event_conn
from .repositories.candidates import _upsert_strategy_domain_result_conn
from .repositories.presets import _empty_preset_domains_page


from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
import json
import os
import sqlite3
import hashlib
import threading
from pathlib import Path
from typing import Any

from .state import has_active_runtime
from .strategy_safety import analyze_strategy

SCHEMA_VERSION = 11
SCHEMA_MIGRATIONS = (
    (1, "base_candidate_storage"),
    (2, "normalized_domain_strategy_model"),
    (3, "minimal_backup_model"),
    (4, "runtime_observability"),
    (5, "remove_legacy_candidate_storage"),
    (6, "preset_domain_state"),
    (7, "compact_runtime_payloads"),
    (8, "trim_strategy_attempt_diagnostics"),
    (9, "minimal_sqlite_working_model"),
    (10, "strategy_analysis_metadata"),
    (11, "app_settings"),
)
_MIGRATION_LOCK = threading.Lock()
_MIGRATED_DB_PATHS: set[Path] = set()
AUTH_BUSY_TIMEOUT_MS = 2_000
_OMITTED = object()
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm")
_RUN_PAYLOAD_COMPACT_BATCH_SIZE = 100
_LEGACY_RUNTIME_FILES = ("available.ndjson", "runs.jsonl", "candidates.json")
_LEGACY_STORAGE_TABLES = (
    "candidate_seen_events",
    "candidate_common_domains",
    "candidate_domains",
    "candidates",
    "presets",
)


class StorageUnavailableError(RuntimeError):
    """A transient SQLite failure that an API adapter may expose as HTTP 503."""

    status_code = 503


_TRANSIENT_SQLITE_PRIMARY_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_IOERR,
    }
)
_TRANSIENT_SQLITE_MESSAGES = (
    "database is locked",
    "database is busy",
    "database schema is locked",
    "disk i/o error",
)


def is_storage_unavailable_error(error: BaseException) -> bool:
    """Return whether *error* is a known temporary SQLite availability failure."""
    if isinstance(error, StorageUnavailableError):
        return True
    if not isinstance(error, sqlite3.OperationalError):
        return False
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int) and (error_code & 0xFF) in _TRANSIENT_SQLITE_PRIMARY_CODES:
        return True
    return any(message in str(error).lower() for message in _TRANSIENT_SQLITE_MESSAGES)


def storage_unavailable_diagnostic(error: BaseException | None) -> dict[str, Any]:
    """Return safe, transport-independent details for an unavailable store.

    This intentionally excludes exception text because SQLite messages may
    include SQL or paths.  API adapters add their own method and path only.
    """
    root = error
    while root is not None and root.__cause__ is not None:
        root = root.__cause__
    extended = getattr(root, "sqlite_errorcode", None)
    return {
        "sqlite_primary_code": (extended & 0xFF) if isinstance(extended, int) else None,
        "sqlite_extended_code": extended if isinstance(extended, int) else None,
        "sqlite_errorname": getattr(root, "sqlite_errorname", None),
        "exception_type": type(root).__name__ if root is not None else "unknown",
    }


def _raise_storage_unavailable(error: sqlite3.OperationalError) -> None:
    """Map only known temporary SQLite availability failures to a stable error."""
    if is_storage_unavailable_error(error):
        raise StorageUnavailableError("storage is temporarily unavailable") from error
    raise error


class ClosingConnection(sqlite3.Connection):
    def __init__(self, database: str | Path, *args: Any, **kwargs: Any) -> None:
        super().__init__(database, *args, **kwargs)
        self._database_path = Path(database)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            try:
                return bool(super().__exit__(exc_type, exc_value, traceback))
            except sqlite3.OperationalError as error:
                _raise_storage_unavailable(error)
        finally:
            self.close()

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def commit(self) -> None:
        try:
            super().commit()
        except sqlite3.OperationalError as error:
            _raise_storage_unavailable(error)

    def close(self) -> None:
        try:
            super().close()
        finally:
            _secure_sqlite_files(self._database_path)


def _set_private_mode(path: Path, mode: int) -> None:
    """Restrict a state path on POSIX without changing Windows ACL handling."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # Existing state directories may be managed by another account or filesystem.
        # Continue operating there rather than breaking an existing installation.
        pass


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIRECTORY_MODE)
    _set_private_mode(path, _PRIVATE_DIRECTORY_MODE)


def _secure_sqlite_files(path: Path) -> None:
    _set_private_mode(path.parent, _PRIVATE_DIRECTORY_MODE)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        _set_private_mode(path.with_name(f"{path.name}{suffix}"), _PRIVATE_FILE_MODE)


def _prepare_sqlite_path(path: Path) -> None:
    if os.name == "posix":
        # SQLite otherwise creates the database using the process umask. Pre-creating
        # it ensures its first inode is owner-only even when that umask is permissive.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, _PRIVATE_FILE_MODE)
        os.close(descriptor)
    _secure_sqlite_files(path)


def db_path(state_dir: Path) -> Path:
    _ensure_private_directory(state_dir)
    root = state_dir / "strategy-finder"
    _ensure_private_directory(root)
    path = root / "state.sqlite3"
    _prepare_sqlite_path(path)
    return path


def connect_api2(state_dir: Path, *, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
    """Open API 2 storage. The legacy runtime must be quiescent before cutover."""
    from .migrations import api2

    path = db_path(state_dir)
    conn = sqlite3.connect(path, timeout=max(0, busy_timeout_ms) / 1000, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        api2.initialize(conn, SCHEMA_MIGRATIONS, legacy_idle=lambda: not _state_has_active_job(state_dir))
        _secure_sqlite_files(path)
        return conn
    except BaseException as error:
        conn.close()
        if isinstance(error, sqlite3.OperationalError):
            _raise_storage_unavailable(error)
        raise

def save_strategy_check(
    state_dir: Path,
    *,
    run_id: str,
    protocol: str,
    execution_format: str,
    args: str,
    files: Mapping[str, bytes],
    outcomes: Mapping[str, bool],
    checkpoint: dict[str, Any] | None,
) -> str | None:
    """Persist the complete check using one FULL SQLite write transaction."""
    from .repositories import strategy_results

    with connect_api2(state_dir) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return strategy_results.save_check(
            conn, state_dir / "strategy-finder" / "assets", run_id=run_id, protocol=protocol,
            execution_format=execution_format, args=args, files=files,
            outcomes=outcomes, checkpoint=checkpoint,
        )

def connect(
    state_dir: Path,
    *,
    check_same_thread: bool = True,
    busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Open storage with the default 30s timeout or a caller-specific budget."""
    try:
        path = db_path(state_dir)
        timeout_seconds = 30 if busy_timeout_ms is None else max(0, busy_timeout_ms) / 1000
        conn = sqlite3.connect(
            path,
            timeout=timeout_seconds,
            factory=ClosingConnection,
            check_same_thread=check_same_thread,
        )
        conn.row_factory = sqlite3.Row
        migration_key = path.resolve()
        with _MIGRATION_LOCK:
            if migration_key not in _MIGRATED_DB_PATHS:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                _migrate_schema(conn)
                _cleanup_runtime_state(conn, path.parent)
                _ensure_system_domain_presets_conn(conn)
                _run_deferred_vacuum(conn, state_dir)
                _MIGRATED_DB_PATHS.add(migration_key)
                _secure_sqlite_files(path)
                return conn
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        _secure_sqlite_files(path)
        return conn
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)


@contextmanager
def auth_transaction(
    state_dir: Path, *, busy_timeout_ms: int = AUTH_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection]:
    """Run a short auth operation under a cross-process SQLite write lock."""
    try:
        timeout_ms = max(0, int(busy_timeout_ms))
    except (TypeError, ValueError):
        timeout_ms = AUTH_BUSY_TIMEOUT_MS
    # sqlite3.connect() installs this busy timeout before the initialization
    # PRAGMAs and migrations below can issue a blocking SQLite operation.
    conn = connect(state_dir, busy_timeout_ms=timeout_ms)
    try:
        # A first connection may have just applied schema migrations. Finish that
        # setup transaction before taking the dedicated auth write lock.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.OperationalError as error:
                _raise_storage_unavailable(error)
            raise
        else:
            conn.commit()
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)
    finally:
        conn.close()


@contextmanager
def auth_read_snapshot(
    state_dir: Path, *, busy_timeout_ms: int = AUTH_BUSY_TIMEOUT_MS
) -> Iterator[sqlite3.Connection | None]:
    """Read an existing auth record without migrations or a writer transaction.

    ``None`` means that the database does not exist yet.  Callers must then use
    :func:`auth_transaction` to perform the initial schema/auth bootstrap.
    """
    try:
        timeout_ms = max(0, int(busy_timeout_ms))
    except (TypeError, ValueError):
        timeout_ms = AUTH_BUSY_TIMEOUT_MS

    path = state_dir / "strategy-finder" / "state.sqlite3"
    if not path.is_file():
        yield None
        return

    conn: sqlite3.Connection | None = None
    try:
        # ``mode=ro`` and ``query_only`` guarantee this path cannot create,
        # migrate, or modify the database.  Autocommit keeps the one SELECT
        # performed by an auth caller as a short WAL reader instead of retaining
        # an explicit transaction until the caller exits the context.
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    except sqlite3.OperationalError as error:
        _raise_storage_unavailable(error)
    finally:
        if conn is not None:
            conn.close()


def storage_runtime_status(state_dir: Path) -> dict[str, Any]:
    path = db_path(state_dir)
    with connect(state_dir) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    schema_version = str(row["value"] or "") if row else ""
    expected_schema_version = str(SCHEMA_VERSION)
    return {
        "db_path": str(path),
        "schema_version": schema_version,
        "expected_schema_version": expected_schema_version,
        "integrity_check": "not_checked",
        "ready": schema_version == expected_schema_version,
        "db_size_bytes": _file_size(path),
    }

def storage_status(state_dir: Path) -> dict[str, Any]:
    path = db_path(state_dir)
    with connect(state_dir) as conn:
        meta = {
            str(row["key"] or ""): str(row["value"] or "")
            for row in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        }
        counts = {table: _table_count(conn, table) for table in _STORAGE_STATUS_TABLES}
        view_counts = {view: _table_count(conn, view) for view in _STORAGE_STATUS_VIEWS}
        migrations = [
            {"version": int(row["version"]), "name": str(row["name"]), "applied_at": str(row["applied_at"])}
            for row in conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()
        ]
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    return {
        "db_path": str(path),
        "schema_version": meta.get("schema_version", ""),
        "expected_schema_version": str(SCHEMA_VERSION),
        "integrity_check": integrity,
        "db_size_bytes": _file_size(path),
        "wal_size_bytes": _file_size(path.with_name(f"{path.name}-wal")),
        "shm_size_bytes": _file_size(path.with_name(f"{path.name}-shm")),
        "tables": counts,
        "views": view_counts,
        "meta": meta,
        "migrations": migrations,
    }


_STORAGE_STATUS_TABLES = (
    "runs",
    "domains",
    "strategies",
    "strategy_domain_results",
    "domain_presets",
    "preset_domains",
    "app_settings",
)

_STORAGE_STATUS_VIEWS = ("domain_stats", "strategy_stats")


def _table_count(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()
    return int(row["count"]) if row else 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _migrate_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS runs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_runs_id_seq ON runs(id, seq);
        CREATE INDEX IF NOT EXISTS idx_runs_seq ON runs(seq);

        CREATE TABLE IF NOT EXISTS domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            service_group TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_domains_name ON domains(name);
        CREATE INDEX IF NOT EXISTS idx_domains_service_group ON domains(service_group);

        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            fragmentation_class TEXT NOT NULL DEFAULT 'unknown',
            fragmentation_safe INTEGER NOT NULL DEFAULT 0,
            fragmentation_reason TEXT NOT NULL DEFAULT '',
            family TEXT NOT NULL DEFAULT 'other',
            family_key TEXT NOT NULL DEFAULT '',
            family_rank INTEGER NOT NULL DEFAULT 900,
            family_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_strategies_protocol ON strategies(protocol);
        CREATE INDEX IF NOT EXISTS idx_strategies_args_hash ON strategies(args_hash);

        CREATE TABLE IF NOT EXISTS strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);

        CREATE TABLE IF NOT EXISTS domain_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user',
            label TEXT NOT NULL DEFAULT '',
            source_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope, name, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_domain_presets_scope_name ON domain_presets(scope, name);

        CREATE TABLE IF NOT EXISTS preset_domains (
            preset_id INTEGER NOT NULL,
            domain_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(preset_id, domain_id),
            FOREIGN KEY(preset_id) REFERENCES domain_presets(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_preset_domains_domain ON preset_domains(domain_id);

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE VIEW IF NOT EXISTS domain_stats AS
        SELECT d.id AS domain_id,
               d.name AS domain,
               COUNT(DISTINCT r.strategy_id) AS strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'tls' THEN r.strategy_id END) AS tls_strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )
    _ensure_column(conn, "strategies", "fragmentation_class", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(conn, "strategies", "fragmentation_safe", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "strategies", "fragmentation_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "strategies", "family", "TEXT NOT NULL DEFAULT 'other'")
    _ensure_column(conn, "strategies", "family_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "strategies", "family_rank", "INTEGER NOT NULL DEFAULT 900")
    _ensure_column(conn, "strategies", "family_reason", "TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategies_family ON strategies(family, family_rank)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategies_fragmentation ON strategies(fragmentation_class)")
    _ensure_column(conn, "domain_presets", "source_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "preset_domains", "enabled", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_position ON preset_domains(preset_id, position)")
    _migrate_minimal_working_model_schema(conn)
    _recreate_stats_views(conn)
    _backfill_strategy_analysis(conn)
    _drop_legacy_storage(conn)
    _compact_run_payloads(conn)
    _drop_strategy_attempts(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    _record_schema_migrations(conn)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def _record_schema_migrations(conn: sqlite3.Connection) -> None:
    for version, name in SCHEMA_MIGRATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name) VALUES(?, ?)",
            (version, name),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_minimal_working_model_schema(conn: sqlite3.Connection) -> bool:
    changed = False
    conn.executescript("DROP VIEW IF EXISTS domain_stats; DROP VIEW IF EXISTS strategy_stats;")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        changed = _migrate_domains_schema(conn) or changed
        changed = _migrate_strategies_schema(conn) or changed
        changed = _migrate_strategy_domain_results_schema(conn) or changed
        changed = _migrate_domain_presets_schema(conn) or changed
        changed = _repair_renamed_foreign_key_targets(conn) or changed
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    if changed:
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError("foreign key check failed after SQLite model migration")
    return changed


def _migrate_domains_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "domains")
    if {"created_at", "updated_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_domains_name;
        DROP INDEX IF EXISTS idx_domains_service_group;
        ALTER TABLE domains RENAME TO domains_old;
        CREATE TABLE domains (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            service_group TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO domains(id, name, service_group)
        SELECT id, name, COALESCE(service_group, '')
        FROM domains_old
        WHERE COALESCE(name, '') != '';
        DROP TABLE domains_old;
        CREATE INDEX IF NOT EXISTS idx_domains_name ON domains(name);
        CREATE INDEX IF NOT EXISTS idx_domains_service_group ON domains(service_group);
        """
    )
    set_meta(conn, "minimal_domains_schema_v9", "1")
    return True


def _migrate_strategies_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "strategies")
    if {"first_seen_at", "last_seen_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategies_last_seen;
        DROP INDEX IF EXISTS idx_strategies_protocol;
        DROP INDEX IF EXISTS idx_strategies_args_hash;
        ALTER TABLE strategies RENAME TO strategies_old;
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            protocol TEXT NOT NULL DEFAULT '',
            args TEXT NOT NULL DEFAULT '',
            args_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate'
        );
        INSERT OR IGNORE INTO strategies(id, protocol, args, args_hash, status)
        SELECT id, COALESCE(protocol, ''), COALESCE(args, ''), COALESCE(args_hash, ''), COALESCE(status, 'candidate')
        FROM strategies_old
        WHERE COALESCE(id, '') != '';
        DROP TABLE strategies_old;
        CREATE INDEX IF NOT EXISTS idx_strategies_protocol ON strategies(protocol);
        CREATE INDEX IF NOT EXISTS idx_strategies_args_hash ON strategies(args_hash);
        """
    )
    conn.execute(
        """
        UPDATE strategies
        SET args_hash = ?
        WHERE COALESCE(args_hash, '') = '' AND COALESCE(args, '') = ''
        """,
        (_args_hash(""),),
    )
    set_meta(conn, "minimal_strategies_schema_v9", "1")
    return True


def _migrate_strategy_domain_results_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "strategy_domain_results")
    legacy_columns = {
        "success_count",
        "fail_count",
        "last_success_run_id",
        "last_fail_run_id",
        "first_seen_at",
        "last_seen_at",
    }
    if legacy_columns.isdisjoint(columns):
        return False
    protocol_expr = "COALESCE(protocol, '')" if "protocol" in columns else "''"
    source_mode_expr = (
        "COALESCE(NULLIF(source_mode, ''), 'single_domain')" if "source_mode" in columns else "'single_domain'"
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_protocol;
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_strategy;
        DROP INDEX IF EXISTS idx_strategy_domain_results_strategy_domain;
        DROP INDEX IF EXISTS idx_strategy_domain_results_source;
        ALTER TABLE strategy_domain_results RENAME TO strategy_domain_results_old;
        CREATE TABLE strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode)
        SELECT strategy_id, domain_id, {protocol_expr}, {source_mode_expr}
        FROM strategy_domain_results_old
        WHERE COALESCE(strategy_id, '') != '' AND domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE strategy_domain_results_old;
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
        """
    )
    set_meta(conn, "minimal_strategy_domain_results_schema_v9", "1")
    return True


def _migrate_domain_presets_schema(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "domain_presets")
    if {"created_at", "updated_at"}.isdisjoint(columns):
        return False
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_domain_presets_scope_name;
        ALTER TABLE domain_presets RENAME TO domain_presets_old;
        CREATE TABLE domain_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'user',
            label TEXT NOT NULL DEFAULT '',
            source_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(scope, name, kind)
        );
        INSERT OR IGNORE INTO domain_presets(id, scope, name, kind, label, source_json)
        SELECT id, COALESCE(scope, ''), COALESCE(name, ''), COALESCE(kind, 'user'), COALESCE(label, ''), COALESCE(source_json, '{}')
        FROM domain_presets_old
        WHERE COALESCE(scope, '') != '' AND COALESCE(name, '') != '';
        DROP TABLE domain_presets_old;
        CREATE INDEX IF NOT EXISTS idx_domain_presets_scope_name ON domain_presets(scope, name);
        """
    )
    set_meta(conn, "minimal_domain_presets_schema_v9", "1")
    return True


def _repair_renamed_foreign_key_targets(conn: sqlite3.Connection) -> bool:
    changed = False
    strategy_refs = _foreign_key_parent_tables(conn, "strategy_domain_results")
    if {"domains_old", "strategies_old"} & strategy_refs:
        _rebuild_strategy_domain_results(conn)
        changed = True
    preset_refs = _foreign_key_parent_tables(conn, "preset_domains")
    if {"domains_old", "domain_presets_old"} & preset_refs:
        _rebuild_preset_domains(conn)
        changed = True
    if changed:
        set_meta(conn, "renamed_foreign_key_targets_repaired_v9", "1")
    return changed


def _foreign_key_parent_tables(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["table"]) for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()}


def _rebuild_strategy_domain_results(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "strategy_domain_results")
    protocol_expr = "COALESCE(old.protocol, '')" if "protocol" in columns else "''"
    source_mode_expr = (
        "COALESCE(NULLIF(old.source_mode, ''), 'single_domain')" if "source_mode" in columns else "'single_domain'"
    )
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_protocol;
        DROP INDEX IF EXISTS idx_strategy_domain_results_domain_strategy;
        DROP INDEX IF EXISTS idx_strategy_domain_results_strategy_domain;
        DROP INDEX IF EXISTS idx_strategy_domain_results_source;
        ALTER TABLE strategy_domain_results RENAME TO strategy_domain_results_fk_old;
        CREATE TABLE strategy_domain_results (
            strategy_id TEXT NOT NULL,
            domain_id INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'single_domain',
            PRIMARY KEY(strategy_id, domain_id, source_mode),
            FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO strategy_domain_results(strategy_id, domain_id, protocol, source_mode)
        SELECT old.strategy_id, old.domain_id, {protocol_expr}, {source_mode_expr}
        FROM strategy_domain_results_fk_old old
        JOIN strategies s ON s.id = old.strategy_id
        JOIN domains d ON d.id = old.domain_id
        WHERE COALESCE(old.strategy_id, '') != '' AND old.domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE strategy_domain_results_fk_old;
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_protocol ON strategy_domain_results(domain_id, protocol);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_domain_strategy ON strategy_domain_results(domain_id, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_strategy_domain ON strategy_domain_results(strategy_id, domain_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_domain_results_source ON strategy_domain_results(source_mode);
        """
    )


def _rebuild_preset_domains(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "preset_domains")
    enabled_expr = "COALESCE(old.enabled, 1)" if "enabled" in columns else "1"
    position_expr = "COALESCE(old.position, 0)" if "position" in columns else "0"
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_preset_domains_domain;
        DROP INDEX IF EXISTS idx_preset_domains_preset_enabled_position;
        DROP INDEX IF EXISTS idx_preset_domains_preset_position;
        ALTER TABLE preset_domains RENAME TO preset_domains_fk_old;
        CREATE TABLE preset_domains (
            preset_id INTEGER NOT NULL,
            domain_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(preset_id, domain_id),
            FOREIGN KEY(preset_id) REFERENCES domain_presets(id) ON DELETE CASCADE,
            FOREIGN KEY(domain_id) REFERENCES domains(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO preset_domains(preset_id, domain_id, position, enabled)
        SELECT old.preset_id, old.domain_id, {position_expr}, {enabled_expr}
        FROM preset_domains_fk_old old
        JOIN domain_presets p ON p.id = old.preset_id
        JOIN domains d ON d.id = old.domain_id
        WHERE old.preset_id IS NOT NULL AND old.domain_id IS NOT NULL
        """
    )
    conn.executescript(
        """
        DROP TABLE preset_domains_fk_old;
        CREATE INDEX IF NOT EXISTS idx_preset_domains_domain ON preset_domains(domain_id);
        CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_enabled_position ON preset_domains(preset_id, enabled, position);
        CREATE INDEX IF NOT EXISTS idx_preset_domains_preset_position ON preset_domains(preset_id, position);
        """
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _recreate_stats_views(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP VIEW IF EXISTS domain_stats;
        DROP VIEW IF EXISTS strategy_stats;
        CREATE VIEW IF NOT EXISTS domain_stats AS
        SELECT d.id AS domain_id,
               d.name AS domain,
               COUNT(DISTINCT r.strategy_id) AS strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'tls' THEN r.strategy_id END) AS tls_strategy_count,
               COUNT(DISTINCT CASE WHEN r.protocol = 'quic' THEN r.strategy_id END) AS quic_strategy_count
        FROM domains d
        LEFT JOIN strategy_domain_results r ON r.domain_id = d.id
        GROUP BY d.id, d.name;

        CREATE VIEW IF NOT EXISTS strategy_stats AS
        SELECT s.id AS strategy_id,
               s.protocol,
               COUNT(DISTINCT r.domain_id) AS domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'single_domain' THEN r.domain_id END) AS single_domain_count,
               COUNT(DISTINCT CASE WHEN r.source_mode = 'multi_domain' THEN r.domain_id END) AS multi_domain_count
        FROM strategies s
        LEFT JOIN strategy_domain_results r ON r.strategy_id = s.id
        GROUP BY s.id, s.protocol;
        """
    )


def _backfill_strategy_analysis(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "strategy_analysis_backfilled_v10") == "1":
        return
    rows = conn.execute(
        """
        SELECT id, protocol, args
        FROM strategies
        WHERE COALESCE(family_key, '') = ''
           OR COALESCE(fragmentation_reason, '') = ''
           OR COALESCE(family_reason, '') = ''
        """
    ).fetchall()
    for row in rows:
        analysis = analyze_strategy(str(row["protocol"] or ""), str(row["args"] or ""))
        conn.execute(
            """
            UPDATE strategies
            SET fragmentation_class = ?,
                fragmentation_safe = ?,
                fragmentation_reason = ?,
                family = ?,
                family_key = ?,
                family_rank = ?,
                family_reason = ?
            WHERE id = ?
            """,
            (
                analysis.fragmentation_class,
                1 if analysis.fragmentation_safe else 0,
                analysis.fragmentation_reason,
                analysis.family,
                analysis.family_key,
                analysis.family_rank,
                analysis.family_reason,
                str(row["id"] or ""),
            ),
        )
    set_meta(conn, "strategy_analysis_backfilled_v10", "1")


def _drop_legacy_storage(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "legacy_storage_removed_v9") == "1":
        return
    removed = sum(1 for table in _LEGACY_STORAGE_TABLES if _table_exists(conn, table))
    set_meta(conn, "legacy_storage_removed_started_v9", "1")
    for table in _LEGACY_STORAGE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    if removed:
        set_meta(conn, "needs_vacuum", "1")
    set_meta(conn, "legacy_storage_removed", "1")
    set_meta(conn, "legacy_storage_removed_v9", "1")
    set_meta(conn, "legacy_storage_removed_tables", str(removed))


def _drop_strategy_attempts(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "strategy_attempts_removed_v9") == "1":
        return
    count = _table_count(conn, "strategy_attempts") if _table_exists(conn, "strategy_attempts") else 0
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_strategy_attempts_strategy;
        DROP INDEX IF EXISTS idx_strategy_attempts_domain;
        DROP INDEX IF EXISTS idx_strategy_attempts_run;
        DROP TABLE IF EXISTS strategy_attempts;
        """
    )
    if count:
        set_meta(conn, "needs_vacuum", "1")
    set_meta(conn, "strategy_attempts_removed_v9", "1")
    set_meta(conn, "strategy_attempts_removed_count", str(count))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None








def _compact_run_payloads(conn: sqlite3.Connection) -> None:
    if get_meta(conn, "run_payloads_compacted_v7") == "1":
        return
    last_seq = _meta_int(conn, "run_payloads_compaction_last_seq_v7")
    changed = _meta_int(conn, "run_payloads_compacted_count")
    original_bytes = _meta_int(conn, "run_payloads_original_bytes")
    compact_bytes = _meta_int(conn, "run_payloads_compact_bytes")
    set_meta(conn, "run_payloads_compaction_started_v7", "1")
    while True:
        rows = conn.execute(
            """
            SELECT seq, payload_json
            FROM runs
            WHERE seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            (last_seq, _RUN_PAYLOAD_COMPACT_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        batch_changed = 0
        for row in rows:
            seq = int(row["seq"])
            raw = str(row["payload_json"] or "")
            raw_bytes = len(raw.encode("utf-8"))
            original_bytes += raw_bytes
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                compact_bytes += raw_bytes
                last_seq = seq
                continue
            if not isinstance(data, dict):
                compact_bytes += raw_bytes
                last_seq = seq
                continue
            compact = compact_run_payload(data)
            payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            compact_bytes += len(payload.encode("utf-8"))
            if payload != raw:
                conn.execute("UPDATE runs SET payload_json = ? WHERE seq = ?", (payload, seq))
                changed += 1
                batch_changed += 1
            last_seq = seq
        set_meta(conn, "run_payloads_compaction_last_seq_v7", str(last_seq))
        set_meta(conn, "run_payloads_compacted_count", str(changed))
        set_meta(conn, "run_payloads_original_bytes", str(original_bytes))
        set_meta(conn, "run_payloads_compact_bytes", str(compact_bytes))
        if batch_changed:
            set_meta(conn, "needs_vacuum", "1")
        conn.commit()
    set_meta(conn, "run_payloads_compacted_v7", "1")
    set_meta(conn, "run_payloads_compacted_count", str(changed))
    set_meta(conn, "run_payloads_original_bytes", str(original_bytes))
    set_meta(conn, "run_payloads_compact_bytes", str(compact_bytes))
    set_meta(conn, "run_payloads_compaction_completed_v7", "1")
    if changed:
        set_meta(conn, "needs_vacuum", "1")
    conn.commit()


def _meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_meta(conn, key) or default)
    except ValueError:
        return default


def _cleanup_runtime_state(conn: sqlite3.Connection, root: Path) -> None:
    if get_meta(conn, "runtime_state_cleaned_v7") != "1":
        has_runtime_data = _table_count(conn, "runs") > 0 or _table_count(conn, "strategies") > 0
        if has_runtime_data:
            for name in _LEGACY_RUNTIME_FILES:
                try:
                    (root / name).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
        set_meta(conn, "runtime_state_cleaned_v7", "1")
    if get_meta(conn, "jobs_jsonl_compacted_v7") == "1":
        return
    for path in dict.fromkeys((root / "jobs.jsonl", root.parent / "jobs.jsonl")):
        _compact_jobs_jsonl(path)
    set_meta(conn, "jobs_jsonl_compacted_v7", "1")


def _compact_jobs_jsonl(path: Path) -> bool:
    if not path.is_file():
        return False
    changed = False
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source, tmp.open("w", encoding="utf-8") as target:
            for line in source:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    target.write(line if line.endswith("\n") else line + "\n")
                    continue
                if isinstance(payload, dict):
                    compact = _compact_job_record(payload)
                    changed = changed or compact != payload
                    target.write(json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
                else:
                    target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        if changed:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
        return changed
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _compact_job_record(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    result = compact.get("result")
    if isinstance(result, dict):
        compact["result"] = compact_run_payload(result)
    return compact


def _run_deferred_vacuum(conn: sqlite3.Connection, state_dir: Path) -> None:
    if get_meta(conn, "needs_vacuum") != "1":
        return
    if _state_has_active_job(state_dir):
        return
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except sqlite3.Error:
        return
    set_meta(conn, "needs_vacuum", "0")
    conn.commit()


def _state_has_active_job(state_dir: Path) -> bool:
    return has_active_runtime(state_dir)

















def append_run(state_dir: Path, run: dict[str, Any]) -> None:
    return _runs_repository.append_run(state_dir, run, _connect=connect)






def read_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return _runs_repository.read_run_payloads(state_dir, limit, offset, _connect=connect)



def read_latest_run_payloads(state_dir: Path, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return _runs_repository.read_latest_run_payloads(state_dir, limit, offset, _connect=connect)



def count_latest_run_payloads(state_dir: Path) -> int:
    return _runs_repository.count_latest_run_payloads(state_dir, _connect=connect)






def upsert_candidate_event(
    state_dir: Path,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    return _candidates_repository.upsert_candidate_event(state_dir, candidate_id=candidate_id, protocol=protocol, args=args, status=status, run_id=run_id, domain=domain, domains=domains, test=test, ip_version=ip_version, seen_at=seen_at, common=common, _connect=connect)



def upsert_candidate_event_conn(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    protocol: str,
    args: str,
    status: str,
    run_id: str,
    domain: str,
    domains: list[str],
    test: str,
    ip_version: str,
    seen_at: str,
    common: bool,
) -> None:
    return _candidates_repository.upsert_candidate_event_conn(conn, candidate_id=candidate_id, protocol=protocol, args=args, status=status, run_id=run_id, domain=domain, domains=domains, test=test, ip_version=ip_version, seen_at=seen_at, common=common)



def read_app_setting(state_dir: Path, key: str) -> Any | None:
    clean_key = str(key or "").strip()
    if not clean_key:
        return None
    with connect(state_dir) as conn:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (clean_key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(str(row["value_json"] or "null"))
    except json.JSONDecodeError:
        return None


def read_or_create_app_setting(state_dir: Path, key: str, value: Any, updated_at: str) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("setting key is required")
    value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (clean_key, value_json, str(updated_at or "")),
        )
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (clean_key,)).fetchone()
    try:
        return json.loads(str(row["value_json"] or "null")) if row else None
    except json.JSONDecodeError:
        return None


def save_app_setting(state_dir: Path, key: str, value: Any, updated_at: str) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("setting key is required")
    value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with connect(state_dir) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (clean_key, value_json, str(updated_at or "")),
        )
    return value


def read_custom_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    return _presets_repository.read_custom_presets(state_dir, _connect=connect)



def read_system_presets(state_dir: Path) -> dict[str, dict[str, list[str]]]:
    return _presets_repository.read_system_presets(state_dir, _connect=connect)



def read_custom_preset_index(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    return _presets_repository.read_custom_preset_index(state_dir, _connect=connect)



def read_system_preset_index(state_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    return _presets_repository.read_system_preset_index(state_dir, _connect=connect)






def save_custom_presets(state_dir: Path, presets: dict[str, Any], updated_at: str) -> dict[str, dict[str, list[str]]]:
    return _presets_repository.save_custom_presets(state_dir, presets, updated_at, _connect=connect)



def save_custom_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domains: list[str],
    updated_at: str,
    source: dict[str, Any] | None = None,
) -> dict[str, dict[str, list[str]]]:
    return _presets_repository.save_custom_preset(state_dir, scope=scope, name=name, domains=domains, updated_at=updated_at, source=source, _connect=connect)



def save_system_preset(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domains: list[str],
    updated_at: str,
) -> dict[str, dict[str, list[str]]]:
    return _presets_repository.save_system_preset(state_dir, scope=scope, name=name, domains=domains, updated_at=updated_at, _connect=connect)



def delete_custom_preset(state_dir: Path, *, scope: str, name: str) -> dict[str, dict[str, dict[str, Any]]]:
    return _presets_repository.delete_custom_preset(state_dir, scope=scope, name=name, _connect=connect)



def delete_user_presets(state_dir: Path, *, scope: str, names: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
    return _presets_repository.delete_user_presets(state_dir, scope=scope, names=names, _connect=connect)



def read_preset_domains_page(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    kind: str = "user",
    query: str = "",
    limit: int = 200,
    offset: int = 0,
    include_disabled: bool = True,
) -> dict[str, Any]:
    return _presets_repository.read_preset_domains_page(state_dir, scope=scope, name=name, kind=kind, query=query, limit=limit, offset=offset, include_disabled=include_disabled, _connect=connect)



def set_preset_domain_enabled(
    state_dir: Path,
    *,
    scope: str,
    name: str,
    domain: str,
    enabled: bool,
    updated_at: str,
    kind: str = "user",
) -> dict[str, Any]:
    return _presets_repository.set_preset_domain_enabled(state_dir, scope=scope, name=name, domain=domain, enabled=enabled, updated_at=updated_at, kind=kind, _connect=connect)













