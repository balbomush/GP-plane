"""Identity and durable files for a captured, ordered nfqws2 composition."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from zipfile import ZIP_DEFLATED, ZipFile
from tempfile import TemporaryDirectory
from collections.abc import Mapping


_FILE_OPTIONS = ("--hostlist", "--hostlist-exclude", "--ipset", "--ipset-exclude")


@dataclass(frozen=True)
class Composition:
    args: str
    fingerprint: str
    identity: bytes
    files: tuple[tuple[str, bytes], ...]


def _file_argument(word: str) -> tuple[str, str] | None:
    option, sep, value = word.partition("=")
    if not sep and option in (*_FILE_OPTIONS, "--blob", "--lua-init"):
        raise ValueError(f"File argument must use {option}=value")
    if option in _FILE_OPTIONS:
        return f"{option}=", value
    if option == "--lua-init" and value.startswith("@"):
        return "--lua-init=@", value[1:]
    if option == "--blob":
        name, separator, body = value.partition(":")
        if not separator or not name:
            raise ValueError("Invalid blob argument")
        if body.startswith("@"):
            return f"--blob={name}:@", body[1:]
    return None


def capture(
    execution_format: str, protocol: str, args: str, files: Mapping[str, bytes],
) -> Composition:
    """Normalize only actual file positions using the captured dependency bytes."""
    if execution_format != "nfqws2" or not protocol:
        raise ValueError("Unknown execution format or protocol")
    words = shlex.split(args, comments=False, posix=True)
    if not words or any("\x00" in word for word in words):
        raise ValueError("Invalid strategy arguments")

    used_paths = set()
    prepared = []
    for index, word in enumerate(words):
        reference = _file_argument(word)
        if reference is None:
            continue
        role, path = reference
        if not path or path not in files or not isinstance(files[path], bytes):
            raise ValueError("Missing captured file for strategy argument")
        used_paths.add(path)
        name = f"{index}.bin"
        words[index] = role + "assets/" + name
        prepared.append((name, files[path]))
    if used_paths != files.keys():
        raise ValueError("Unbound strategy dependency")
    identity = json.dumps(
        [1, execution_format, protocol, words, [
            [name, hashlib.sha256(content).hexdigest()] for name, content in prepared
        ]],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(identity).hexdigest()
    return Composition(shlex.join(words), fingerprint, identity, tuple(prepared))


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _matches(path: Path, content: bytes) -> bool:
    return not path.is_symlink() and path.is_file() and path.read_bytes() == content


def persist(root: Path, composition: Composition) -> str:
    """Make every required file durable before returning its storage reference."""
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Strategy assets root must be a real directory")
    target = root / composition.fingerprint
    if target.is_symlink():
        raise ValueError("Strategy assets directory must not be a symlink")
    if target.exists():
        files_dir = target / "assets"
        if (files_dir.is_symlink() or not _matches(target / "identity.json", composition.identity)
                or not all(_matches(files_dir / name, content) for name, content in composition.files)):
            raise ValueError("Stored strategy composition differs from the captured composition")
    else:
        with TemporaryDirectory(prefix=".strategy-", dir=root) as temporary:
            stage = Path(temporary) / "composition"
            files_dir = stage / "assets"
            files_dir.mkdir(parents=True)
            for name, content in composition.files:
                _write_synced(files_dir / name, content)
            _write_synced(stage / "identity.json", composition.identity)
            _sync_directory(files_dir)
            _sync_directory(stage)
            stage.rename(target)
    _sync_directory(root)
    _sync_directory(root.parent)
    return composition.fingerprint


def load(
    root: Path, reference: str | None, fingerprint: str | None, *,
    expected_args: str | None, verify: bool,
) -> tuple[str, dict[str, bytes]]:
    """Read and validate the retained composition without following external paths."""
    if (not isinstance(reference, str) or reference != fingerprint
            or not re.fullmatch(r"[0-9a-f]{64}", reference)):
        raise ValueError("No complete composition")
    target = root / reference
    identity_path = target / "identity.json"
    directory = target / "assets"
    if any(path.is_symlink() for path in (root, target, identity_path, directory)):
        raise ValueError("Unsafe strategy asset path")
    identity = identity_path.read_bytes()
    if hashlib.sha256(identity).hexdigest() != fingerprint:
        raise ValueError("Invalid composition identity")
    version, execution_format, protocol, words, entries = json.loads(identity)
    if version != 1 or execution_format != "nfqws2" or not protocol or not isinstance(words, list):
        raise ValueError("Unsupported composition identity")
    if not words or not all(isinstance(word, str) for word in words):
        raise ValueError("Invalid composition words")
    paths = set()
    for word in words:
        file_arg = _file_argument(word)
        if file_arg is not None:
            paths.add(file_arg[1])
    args = shlex.join(words)
    if expected_args is not None and args != expected_args:
        raise ValueError("Stored arguments differ from the composition")
    files = {}
    for name, digest in entries:
        if not re.fullmatch(r"[0-9]+\.bin", name) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid composition asset")
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Composition asset is missing")
        contents = path.read_bytes() if verify else b""
        if verify and hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError("Invalid composition asset hash")
        files["assets/" + name] = contents
    if files.keys() != paths:
        raise ValueError("Composition contains missing or unbound assets")
    return args, files


def write_archive(output, kind: str, args: str, files: dict[str, bytes]) -> None:
    entrypoint = "strategy.args" if kind == "strategy" else "bundle.conf"
    contents = {entrypoint: (args + "\n").encode("utf-8"), **files}
    manifest = {
        "schema_version": 1, "kind": kind, "target": "nfqws2", "entrypoint": entrypoint,
        "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in contents.items()
        ],
    }
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path, data in contents.items():
            archive.writestr(path, data)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr(
            "README.txt",
            f"{entrypoint} contains ordered nfqws2 arguments as POSIX shell words.\n"
            "Parse the words without executing a shell. File arguments refer to paths from the ZIP root.\n"
            "Validate the hashes in manifest.json before applying the composition.\n",
        )
