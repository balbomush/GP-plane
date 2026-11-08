"""The user-data subset of the Core snapshot archive for API 2."""

from contextlib import closing
from pathlib import Path, PurePosixPath
import json
import os
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from . import backups, storage, strategy_assets
from .migrations import api2
from .resource_budget import BACKUP_UPLOAD_MAX_BYTES, BACKUP_STREAM_CHUNK_BYTES
from .state import now_iso


FORMAT = "8"
USER_TABLES = (
    "engines", "milestones", "domains", "domain_presets", "strategies", "bundles",
    "preset_domains", "strategy_domain_results", "bundle_domain_results",
    "strategy_result_engines", "runs",
)
_ARCHIVE_LIMIT = BACKUP_UPLOAD_MAX_BYTES


class BackupTooLarge(ValueError):
    pass


def saved_path(state_dir: Path) -> Path:
    return backups.backups_dir(state_dir) / "user.zip"


def _asset_root(state_dir):
    return state_dir / "strategy-finder" / "assets"


def _export(conn, asset_root, folder):
    with (folder / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump({"schema_version": FORMAT, "created_at": now_iso(), "completed": "true"}, file)
    tables = folder / "data"
    tables.mkdir()
    for table in USER_TABLES:
        with (tables / (table + ".ndjson")).open("w", encoding="utf-8") as file:
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                file.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    for reference, composition in _compositions(conn, asset_root).items():
        strategy_assets.persist(folder / "assets", composition)


def _compositions(conn, root):
    compositions = {}
    for reference, fingerprint, args in conn.execute(
        """SELECT assets_ref, fingerprint, args FROM strategies WHERE assets_ref IS NOT NULL OR fingerprint IS NOT NULL
           UNION ALL SELECT files_ref, fingerprint, NULL FROM bundles"""
    ):
        portable, files = strategy_assets.load(root, reference, fingerprint, expected_args=args, verify=True)
        directory = root / reference
        identity = (directory / "identity.json").read_bytes()
        compositions[reference] = strategy_assets.Composition(
            portable, fingerprint, identity, tuple((PurePosixPath(path).name, data) for path, data in files.items()),
        )
    return compositions


def _zip(folder, output):
    backups._write_checksums(folder)
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    if sum(path.stat().st_size for path in files) > _ARCHIVE_LIMIT:
        raise BackupTooLarge("Backup exceeds unpacked limit")
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "snapshot/" + path.relative_to(folder).as_posix())
    if output.stat().st_size > _ARCHIVE_LIMIT:
        raise BackupTooLarge("Backup exceeds upload limit")


def create(state_dir: Path) -> dict:
    destination = saved_path(state_dir)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".user-backup-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        folder = stage / "snapshot"
        folder.mkdir()
        with storage.connect_api2(state_dir) as conn:
            conn.execute("BEGIN")
            _export(conn, _asset_root(state_dir), folder)
        path = stage / "user.zip"
        _zip(folder, path)
        payload = {
            "created_at": backups._read_manifest(folder / "manifest.json")["created_at"],
            "size_bytes": path.stat().st_size, "sha256": backups._sha256_file(path),
        }
        with path.open("r+b") as file:
            os.fsync(file.fileno())
        os.replace(path, destination)
        backups._fsync_directory(destination.parent)
    return payload


def _unpack(archive, folder):
    seen = set()
    top = None
    total = 0
    with ZipFile(archive) as zip_file:
        for item in zip_file.infolist():
            name = item.filename
            parts = name.split("/")
            if (len(parts) < 2 or any(not part or part in (".", "..") for part in parts)
                    or any(ch in name for ch in "\\:\x00")
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", name)
                    or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError("Invalid backup member")
            top = top or parts[0]
            relative = "/".join(parts[1:])
            if parts[0] != top or relative.lower() in seen:
                raise ValueError("Duplicate or inconsistent backup path")
            seen.add(relative.lower())
            total += item.file_size
            if total > _ARCHIVE_LIMIT:
                raise BackupTooLarge("Unpacked backup exceeds limit")
            path = folder.joinpath(*parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(item) as source, path.open("xb") as target:
                copied = 0
                while data := source.read(BACKUP_STREAM_CHUNK_BYTES):
                    copied += len(data)
                    if copied > item.file_size:
                        raise ValueError("Backup member size mismatch")
                    target.write(data)
            if copied != item.file_size:
                raise ValueError("Backup member size mismatch")
    checksums = folder / "checksums.sha256"
    if not checksums.is_file() or not backups._verify_snapshot_path(folder):
        raise ValueError("Backup checksum verification failed")
    paths = set(line.partition("  ")[2].lower() for line in checksums.read_text(encoding="utf-8").splitlines())
    if paths | {"checksums.sha256"} != seen:
        raise ValueError("Backup checksum coverage is incomplete")


def _replace_rows(source, target):
    for table in reversed(USER_TABLES):
        target.execute(f"DELETE FROM {table}")
    for table in USER_TABLES:
        columns = api2.V12_COLUMNS[table].split()
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        target.executemany(
            f"INSERT INTO {table}({names}) VALUES({placeholders})",
            source.execute(f"SELECT {names} FROM {table}"),
        )


def _load_rows(folder, conn):
    for table in reversed(USER_TABLES):
        conn.execute(f"DELETE FROM {table}")
    for table in USER_TABLES:
        columns = api2.V12_COLUMNS[table].split()
        placeholders = ", ".join("?" for _ in columns)
        with (folder / "data" / (table + ".ndjson")).open(encoding="utf-8") as file:
            for line in file:
                row = json.loads(line)
                if not isinstance(row, dict) or row.keys() != set(columns):
                    raise ValueError(f"Invalid backup row in {table}")
                conn.execute(f"INSERT INTO {table}({', '.join(columns)}) VALUES({placeholders})",
                             [row[name] for name in columns])
    if conn.execute("SELECT max(id) FROM milestones").fetchone()[0] in (None, 0):
        raise ValueError("Backup has no current milestone")


def _legacy_rows(folder, stage, conn):
    plan = backups._load_restore_plan(folder)
    plan["restore_settings"] = False
    plan["app_settings"] = []
    legacy_state = stage / "legacy-state"
    # Use the existing supported decoder on a private scratch database,
    # then reuse the same v11-to-v12 conversion as an ordinary update.
    backups._restore_snapshot_plan(legacy_state, "api2-conversion", plan, {})
    with storage.connect_api2(legacy_state) as old:
        _replace_rows(old, conn)


def restore(state_dir: Path, archive, stage: Path) -> None:
    folder = stage / "unpacked"
    folder.mkdir()
    try:
        _unpack(archive, folder)
    except BadZipFile as error:
        raise ValueError("Invalid backup ZIP") from error
    manifest = backups._read_manifest(folder / "manifest.json")
    if manifest.get("completed") != "true":
        raise ValueError("Incomplete backup")
    version = manifest.get("schema_version")
    with closing(sqlite3.connect(":memory:")) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA foreign_keys=ON")
        api2._create(source)
        if version == FORMAT:
            _load_rows(folder, source)
        elif version == "7":
            _legacy_rows(folder, stage, source)
        else:
            raise ValueError("Unsupported backup version")
        message = json.dumps({
            "code": "interrupted", "message": "Restored unfinished search",
        }, separators=(",", ":"))
        source.execute(
            """UPDATE runs SET status='failed', error_json=?, finished_at=NULL
               WHERE status IN ('starting','running','stopping')""", (message,),
        )
        for composition in _compositions(source, folder / "assets").values():
            strategy_assets.persist(_asset_root(state_dir), composition)
        with storage.connect_api2(state_dir) as target:
            target.execute("BEGIN IMMEDIATE")
            _replace_rows(source, target)
