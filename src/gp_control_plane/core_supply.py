"""Prepare and verify the exact published Core distribution for local use."""

from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.error import HTTPError

from . import releases
from .backups import _fsync_directory


_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?"
_TAG = re.compile(r"v" + _VERSION + r"(?:-alpha\.[0-9]+)?")
_WHEEL = re.compile(r"gp_access_control_plane-(" + _VERSION + r")-py3-none-any\.whl")
_SOURCE = re.compile(r"core-[0-9a-f]{40}\.tar\.gz")
_CHECKSUM = re.compile(r"([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]+)")
_MAX_ASSET = 64 * 1024 * 1024
_MAX_SUMS = 64 * 1024


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
    wheels = [(name, match[1]) for name in names if (match := _WHEEL.fullmatch(name))]
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
            from hashlib import file_digest
            actual = file_digest(file, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Release checksum mismatch")
        total += path.stat().st_size
    return total


def prepare(state_dir: Path, version: str) -> int | None:
    if not re.fullmatch(_VERSION, version):
        return None
    store = state_dir / "strategy-finder" / "downloads"
    target = store / version
    if target.exists() or target.is_symlink():
        return _valid_size(target, version)
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
        stage.rename(target)
        _fsync_directory(store)
    return size


def is_rate_limit(error):
    return isinstance(error, HTTPError) and (
        error.code == 429 or error.code == 403 and error.headers is not None
        and error.headers.get("X-RateLimit-Remaining") == "0"
    )
