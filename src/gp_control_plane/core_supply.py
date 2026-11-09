"""Prepare and verify the exact published Core distribution for local use."""

from base64 import urlsafe_b64encode
from configparser import ConfigParser, Error as IniError
import csv
from email.parser import BytesParser
from hashlib import file_digest, sha256
import os
from pathlib import Path, PurePosixPath
from tarfile import TarError, open as open_tar
import tomllib
from zipfile import BadZipFile, ZipFile
import re
from tempfile import TemporaryDirectory
from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from .downloads import DownloadError

from . import releases
from .backups import _fsync_directory


_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?"
_TAG = re.compile(r"v" + _VERSION + r"(?:-alpha\.[0-9]+)?")
_SOURCE = re.compile(r"core-[0-9a-f]{40}\.tar\.gz")
_CHECKSUM = re.compile(r"([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]+)")
_MAX_ASSET = 64 * 1024 * 1024
_MAX_SUMS = 64 * 1024


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


def catalog():
    return [
        info for row in releases.parse_github_releases(releases.fetch_github_releases())
        if (info := _release_info(row)) is not None
    ]


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


def prepare(state_dir: Path, version: str) -> int | None:
    if not re.fullmatch(_VERSION, version):
        return None
    store = state_dir / "strategy-finder" / "downloads"
    target = store / version
    if store.is_symlink() or target.is_symlink():
        raise ValueError("Unsafe release cache")
    if target.exists():
        if not target.is_dir():
            raise ValueError("Release cache path is not a directory")
        try:
            return _valid_size(target, version)
        except (OSError, ValueError):
            # A damaged local supply is not a usable release. Leave it
            # in place until a new complete supply has been validated.
            pass
    candidates = [(info, files) for info, files in catalog() if info["version"] == version]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError("Ambiguous version in release catalog")
    info, files = candidates[0]
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if store.is_symlink():
        raise ValueError("Unsafe release cache")
    with TemporaryDirectory(prefix=".release-", dir=store) as temporary:
        stage = Path(temporary) / "delivery"
        stage.mkdir()
        sums_raw = releases.fetch_release_asset(info["tag"], "SHA256SUMS", files["SHA256SUMS"])
        sums = _sums(sums_raw)
        if sums.keys() != files.keys() - {"SHA256SUMS"}:
            raise ValueError("Release checksums do not cover the complete supply")
        (stage / "SHA256SUMS").write_bytes(sums_raw)
        for name, digest in sums.items():
            data = releases.fetch_release_asset(info["tag"], name, files[name])
            if len(data) != files[name] or sha256(data).hexdigest() != digest:
                raise ValueError("Release checksum mismatch")
            (stage / name).write_bytes(data)
        size = _valid_size(stage, version)
        previous = Path(temporary) / "previous"
        if target.exists():
            target.rename(previous)
        try:
            stage.rename(target)
        except OSError:
            if previous.exists():
                os.replace(previous, target)
            raise
        _fsync_directory(store)
    return size


def is_rate_limit(error):
    return isinstance(error, DownloadError) and error.rate_limited
