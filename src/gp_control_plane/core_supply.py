"""Prepare and verify the exact published Core distribution for local use."""

from base64 import urlsafe_b64encode
from configparser import ConfigParser, Error as IniError
from datetime import datetime, timezone
import csv
import json
from email.parser import BytesParser
from hashlib import file_digest, sha256
import os
from pathlib import Path, PurePosixPath
from tarfile import TarError, open as open_tar
import tomllib
from zipfile import BadZipFile, ZipFile
import re
from tempfile import TemporaryDirectory
from threading import RLock
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from .downloads import DownloadError

from . import releases
from .backups import _fsync_directory, _write_private_json_atomic


_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?"
_TAG = re.compile(r"v" + _VERSION + r"(?:-alpha\.[0-9]+)?")
_SOURCE = re.compile(r"core-[0-9a-f]{40}\.tar\.gz")
_CHECKSUM = re.compile(r"([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]+)")
_MAX_ASSET = 64 * 1024 * 1024
_MAX_SUMS = 64 * 1024
_CACHE_PUBLICATION = RLock()


def _wheel_version(filename: str) -> str | None:
    try:
        project, version, build, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return None
    if project != "gp-access-control-plane" or build or tags != {Tag("py3", "none", "any")}:
        return None
    # Match the exact published artifact spelling, not only its normalized identity.
    if filename != f"gp_access_control_plane-{version}-py3-none-any.whl" or not re.fullmatch(_VERSION, str(version)):
        return None
    return str(version)


def _release_info(row):
    tag = row.get("tag_name")
    prerelease = row.get("prerelease")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag) or type(prerelease) is not bool:
        return None
    assets = row.get("assets")
    if not isinstance(assets, list):
        return None
    names = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        name, size = asset["name"], asset.get("size")
        if name in names:
            return None
        if type(size) is int and 0 < size <= _MAX_ASSET:
            names[name] = size
    wheels = [(name, version) for name in names if (version := _wheel_version(name)) is not None]
    sources = [name for name in names if _SOURCE.fullmatch(name)]
    if len(wheels) != 1 or len(sources) != 1 or not 0 < names.get("SHA256SUMS", 0) <= _MAX_SUMS:
        return None
    wheel, version = wheels[0]
    sdist = f"gp_access_control_plane-{version}.tar.gz"
    if sdist not in names:
        return None
    return {"tag": tag, "version": version, "prerelease": prerelease}, {
        name: names[name] for name in (wheel, sdist, sources[0], "SHA256SUMS")
    }


_STAGES = {"alpha", "prerelease", "stable"}
_STAGE_MARKER = re.compile(r"<!--\s*gp-release-stage:\s*([a-z]+)\s*-->")


def _pair(version, tag):
    if not isinstance(version, str) or len(version) > 64 or not re.fullmatch(_VERSION, version) or tag not in _STAGES:
        raise ValueError("Invalid Core version or stage")


def _record(row):
    git_tag = row.get("tag_name")
    if not isinstance(git_tag, str) or not _TAG.fullmatch(git_tag):
        return None
    version = git_tag.removeprefix("v").split("-alpha.")[0]
    body = row.get("body") or ""
    marks = _STAGE_MARKER.findall(body)
    prerelease = row.get("prerelease")
    if type(prerelease) is not bool or len(marks) > 1 or ("gp-release-stage" in body and len(marks) != 1):
        raise ValueError("Ambiguous Core release stage")
    stage = marks[0] if marks else ("alpha" if git_tag == "v0.5.0" and prerelease else "prerelease" if prerelease else "stable")
    if stage not in _STAGES or prerelease != (stage != "stable"):
        raise ValueError("Core release stage contradicts GitHub metadata")
    identifier, published = row.get("id"), row.get("published_at")
    if type(identifier) is not int or identifier <= 0 or not isinstance(published, str):
        raise ValueError("Missing Core release publication identity")
    try:
        timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Missing publication timezone")
    except ValueError as error:
        raise ValueError("Invalid Core release publication time") from error
    complete = _release_info(row)
    if complete is not None and complete[0]["version"] != version:
        raise ValueError("Core release package version contradicts its reference")
    info = {"version": version, "tag": stage, "git_tag": git_tag, "release_id": identifier,
            "published_at": timestamp.astimezone(timezone.utc).isoformat()}
    return info, complete[1] if complete is not None else None


def catalog():
    records = []
    seen = set()
    for row in releases.parse_github_releases(releases.fetch_github_releases()):
        record = _record(row)
        if record is not None:
            if record[0]["release_id"] in seen:
                raise ValueError("Duplicate Core release identity")
            seen.add(record[0]["release_id"])
            records.append(record)
    return records


def _latest(records, version, tag):
    matches = [(info, files) for info, files in records if info["version"] == version and info["tag"] == tag]
    if not matches:
        return None
    selected = max(matches, key=lambda record: (record[0]["published_at"], record[0]["release_id"]))
    if selected[1] is None:
        raise ValueError("Latest Core release supply is incomplete")
    return selected


def latest(version, tag):
    _pair(version, tag)
    return _latest(catalog(), version, tag)


def list_releases(tag):
    if tag not in _STAGES:
        raise ValueError("Invalid Core stage")
    records = catalog()
    versions = {info["version"] for info, _ in records if info["tag"] == tag}
    from packaging.version import Version
    # The public list identifies pairs, not downloadable files. An incomplete
    # legacy version must not hide other versions; explicit preparation still
    # validates the latest publication and never falls back to an older build.
    return [{"version": version, "tag": tag} for version in sorted(versions, key=Version, reverse=True)]


def _sums(raw: bytes) -> dict[str, str]:
    result = {}
    for line in raw.decode("ascii").splitlines():
        match = _CHECKSUM.fullmatch(line)
        if match is None or match[2] in result:
            raise ValueError("Invalid release checksums")
        result[match[2]] = match[1]
    return result


def _package_metadata(data: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(data, headersonly=True)
    if metadata.get_all("Name") != ["gp-access-control-plane"] or metadata.get_all("Version") != [version]:
        raise ValueError("Package name or version does not match the release")


def _tar_files(path: Path, names: set[str]) -> dict[str, bytes]:
    selected = {}
    total = 0
    with open_tar(path, "r:gz") as archive:
        for member in archive:
            path_parts = PurePosixPath(member.name)
            if path_parts.is_absolute() or ".." in path_parts.parts or not (member.isdir() or member.isfile()):
                raise ValueError("Unsafe release archive member")
            total += member.size
            if total > 4 * _MAX_ASSET:
                raise ValueError("Release archive expands beyond its limit")
            if member.name in names:
                if member.name in selected or not member.isfile() or member.size > _MAX_SUMS:
                    raise ValueError("Invalid release metadata member")
                with archive.extractfile(member) as file:
                    selected[member.name] = file.read()
    if selected.keys() != names:
        raise ValueError("Release archive metadata missing")
    return selected


def _verify_wheel(path: Path, project: str, version: str) -> None:
    info_dir = project + ".dist-info"
    required = (
        f"{info_dir}/METADATA", f"{info_dir}/WHEEL", f"{info_dir}/entry_points.txt",
        "gp_control_plane/__init__.py",
        "gp_control_plane/cli.py", "gp_control_plane/http/openapi.json",
    )
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError("Duplicate wheel member")
        if sum(item.file_size for item in entries) > 4 * _MAX_ASSET:
            raise ValueError("Wheel expands beyond its limit")
        record = archive.getinfo(f"{info_dir}/RECORD")
        if record.file_size > _MAX_ASSET:
            raise ValueError("Wheel record exceeds its limit")
        rows = list(csv.reader(archive.read(record).decode("utf-8").splitlines()))
        if any(len(row) != 3 for row in rows) or len({row[0] for row in rows}) != len(rows):
            raise ValueError("Invalid wheel record")
        checksums = {name: (digest, size) for name, digest, size in rows}
        data = {}
        for name in required:
            item = archive.getinfo(name)
            if item.is_dir() or not 0 < item.file_size <= _MAX_ASSET:
                raise ValueError("Required wheel member is missing")
            contents = archive.read(item)
            digest = urlsafe_b64encode(sha256(contents).digest()).decode("ascii").rstrip("=")
            if checksums.get(name) != (f"sha256={digest}", str(len(contents))):
                raise ValueError("Required wheel member does not match its record")
            data[name] = contents
        _package_metadata(data[f"{info_dir}/METADATA"], version)
        wheel = BytesParser().parsebytes(data[f"{info_dir}/WHEEL"], headersonly=True)
        if wheel.get("Wheel-Version") != "1.0" or "py3-none-any" not in wheel.get_all("Tag", []):
            raise ValueError("Unexpected wheel compatibility")
        entry_points = ConfigParser(interpolation=None)
        entry_points.optionxform = str
        entry_points.read_string(data[f"{info_dir}/entry_points.txt"].decode("utf-8"))
        entry_points.defaults().clear()
        if entry_points.get("console_scripts", "gp-control-plane", fallback=None) != "gp_control_plane.cli:main":
            raise ValueError("Wheel lacks the Core command")


def _verify_project(raw: bytes, version: str) -> None:
    fields = tomllib.loads(raw.decode("utf-8")).get("project", {})
    if not isinstance(fields, dict):
        raise ValueError("Project metadata must be a table")
    scripts = fields.get("scripts", {})
    if (fields.get("name") != "gp-access-control-plane" or fields.get("version") != version
            or not isinstance(scripts, dict) or scripts.get("gp-control-plane") != "gp_control_plane.cli:main"):
        raise ValueError("Project identity does not match the Core release")


def _verify_identity(root: Path, version: str, source: str) -> None:
    project = f"gp_access_control_plane-{version}"
    wheel = root / f"{project}-py3-none-any.whl"
    sdist = root / f"{project}.tar.gz"
    try:
        _verify_wheel(wheel, project, version)
        files = {
            "pyproject.toml", "setup.py", "scripts/install-linux.sh",
            "src/gp_control_plane/__init__.py", "src/gp_control_plane/cli.py",
        }
        source_files = _tar_files(root / source, files)
        package_files = _tar_files(sdist, {f"{project}/{name}" for name in files | {"PKG-INFO"}})
        _package_metadata(package_files[f"{project}/PKG-INFO"], version)
        _verify_project(package_files[f"{project}/pyproject.toml"], version)
        _verify_project(source_files["pyproject.toml"], version)
    except (BadZipFile, TarError, KeyError, EOFError, IniError) as error:
        raise ValueError("Invalid release package format") from error


def _valid_size(root: Path, version: str) -> int:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("No safe cached supply")
    manifest = root / "SHA256SUMS"
    if manifest.is_symlink() or manifest.stat().st_size > _MAX_SUMS:
        raise ValueError("Invalid checksum manifest")
    sums = _sums(manifest.read_bytes())
    required = {
        f"gp_access_control_plane-{version}-py3-none-any.whl",
        f"gp_access_control_plane-{version}.tar.gz",
    }
    sources = [name for name in sums if _SOURCE.fullmatch(name)]
    if len(sources) != 1 or sums.keys() != required.union(sources):
        raise ValueError("Supply does not match the requested version")
    total = 0
    for name, expected in sums.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= _MAX_ASSET:
            raise ValueError("Invalid cached asset")
        with path.open("rb") as file:
            actual = file_digest(file, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Release checksum mismatch")
        total += path.stat().st_size
    _verify_identity(root, version, sources[0])
    return total


def _cache_pair(state_dir, version, tag):
    _pair(version, tag)
    parent = Path(state_dir) / "strategy-finder"
    for component in (parent, parent / "downloads", parent / "downloads" / version,
                      parent / "downloads" / version / tag):
        if component.is_symlink() or (component.exists() and not component.is_dir()):
            raise ValueError("Unsafe release cache")
    return parent / "downloads" / version / tag


def pinned_path(state_dir, version, tag, supply):
    """Validate the exact update binding without consulting GitHub or a selector."""
    parent = _cache_pair(state_dir, version, tag)
    if not isinstance(supply, dict) or supply.get("version") != version or supply.get("tag") != tag:
        raise ValueError("Supply belongs to another version or stage")
    identifier, digest = supply.get("release_id"), supply.get("checksums_sha256")
    if (type(identifier) is not int or identifier <= 0 or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or supply.get("directory") != f"{identifier}-{digest}"):
        raise ValueError("Invalid pinned supply identity")
    target = parent / supply["directory"]
    if target.is_symlink() or not target.is_dir():
        raise ValueError("No safe pinned supply")
    if (sha256((target / "SHA256SUMS").read_bytes()).hexdigest() != digest
            or _valid_size(target, version) != supply.get("size_bytes")):
        raise ValueError("Pinned supply changed")
    sums = _sums((target / "SHA256SUMS").read_bytes())
    if f"core-{supply.get('source_sha')}.tar.gz" not in sums:
        raise ValueError("Pinned source identity changed")
    git_tag = supply.get("git_tag")
    if not isinstance(git_tag, str) or not _TAG.fullmatch(git_tag):
        raise ValueError("Invalid pinned Git reference")
    return target


def for_update(state_dir, version, tag):
    """Install the selected verified local supply, resolving only if none exists."""
    pointer = _cache_pair(state_dir, version, tag) / "selected.json"
    if pointer.is_symlink():
        raise ValueError("Unsafe release selection")
    if pointer.exists():
        if pointer.stat().st_size > _MAX_SUMS:
            raise ValueError("Invalid release selection")
        supply = json.loads(pointer.read_bytes())
        pinned_path(state_dir, version, tag, supply)
        return supply
    return prepare(state_dir, version, tag)


def prepare(state_dir: Path, version: str, tag: str):
    """An explicit download always resolves the latest publication of this pair."""
    parent = _cache_pair(state_dir, version, tag)
    selected = latest(version, tag)
    if selected is None:
        return None
    info, files = selected
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _cache_pair(state_dir, version, tag)
    pointer = parent / "selected.json"
    if pointer.is_symlink():
        raise ValueError("Unsafe release selection")
    sums_raw = releases.fetch_release_asset(info["git_tag"], "SHA256SUMS", files["SHA256SUMS"])
    sums = _sums(sums_raw)
    if sums.keys() != files.keys() - {"SHA256SUMS"}:
        raise ValueError("Release checksums do not cover the complete supply")
    digest = sha256(sums_raw).hexdigest()
    source = next(name for name in files if _SOURCE.fullmatch(name))
    supply = {"version": version, "tag": tag, "release_id": info["release_id"],
              "git_tag": info["git_tag"], "source_sha": source[5:-7],
              "checksums_sha256": digest, "directory": f"{info['release_id']}-{digest}",
              "size_bytes": sum(size for name, size in files.items() if name != "SHA256SUMS")}
    target = parent / supply["directory"]
    if target.exists() or target.is_symlink():
        try:
            pinned_path(state_dir, version, tag, supply)
        except (OSError, ValueError):
            if target.is_symlink() or not target.is_dir():
                raise ValueError("Unsafe release cache target")
        else:
            _write_private_json_atomic(pointer, supply)
            return supply
    with TemporaryDirectory(prefix=".release-", dir=parent) as temporary:
        stage = Path(temporary) / "delivery"
        stage.mkdir()
        (stage / "SHA256SUMS").write_bytes(sums_raw)
        for name, digest in sums.items():
            data = releases.fetch_release_asset(info["git_tag"], name, files[name])
            if len(data) != files[name] or sha256(data).hexdigest() != digest:
                raise ValueError("Release checksum mismatch")
            (stage / name).write_bytes(data)
        _valid_size(stage, version)
        with _CACHE_PUBLICATION:
            # Another preparation may have published this exact bundle while
            # network I/O was in progress. Never move a valid update binding.
            reusable = False
            if target.exists() or target.is_symlink():
                try:
                    pinned_path(state_dir, version, tag, supply)
                    reusable = True
                except (OSError, ValueError):
                    if target.is_symlink() or not target.is_dir():
                        raise ValueError("Unsafe release cache target")
            if not reusable:
                previous = Path(temporary) / "previous"
                if target.exists():
                    target.rename(previous)
                try:
                    stage.rename(target)
                except OSError:
                    if previous.exists():
                        os.replace(previous, target)
                    raise
                _fsync_directory(parent)
            pinned_path(state_dir, version, tag, supply)
            _write_private_json_atomic(pointer, supply)
    return supply


def is_rate_limit(error):
    return isinstance(error, DownloadError) and error.rate_limited
