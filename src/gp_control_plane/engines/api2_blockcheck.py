"""Adapt the existing managed blockcheck2 processes to API 2 observations."""

from base64 import b64decode
from hashlib import file_digest
from hashlib import sha256
import os
from pathlib import Path
import re
import shlex
import shutil
from tempfile import TemporaryFile
from uuid import uuid4

from ..discovery_parsing import _protocol_from_test
from ..strategy_assets import _file_argument
from ..strategy_finder import (
    DiscoveryOptions, _ipvs_value, _resolve_blockcheck_script, _run_process_with_live_stdout,
)
from ..zapret2 import BLOCKCHECK_ENV_KEYS, root_command, root_helper_status


class _Observations:
    def __init__(self, job, base, marker):
        self.job = job
        self.base = base
        self.prefix = f"GP-API2 {marker} "
        self.pending = {}
        self.group = None

    def mark_phase(self, phase):
        pass

    def record_line(self, line):
        if not line.startswith(self.prefix):
            self.job.log(line)
            return
        command, _, arguments = line.rstrip("\r\n").removeprefix(self.prefix).partition(" ")
        if command == "begin":
            identity, probe, encoded = arguments.split()
            words = b64decode(encoded, validate=True).decode("utf-8").split("\0")
            if self.group is not None or words[-1] or not re.fullmatch("[a-f0-9]{64}", identity):
                raise ValueError("Invalid engine observation")
            self.group = (identity, _protocol_from_test(probe), words[:-1], {})
        elif command == "domain":
            domain, code = arguments.split()
            if self.group is None or domain not in self.job.domains or domain in self.group[3]:
                raise ValueError("Invalid engine observation domain")
            self.group[3][domain] = int(code) == 0
        elif command == "end" and self.group is not None:
            self._finish_group()
        else:
            raise ValueError("Invalid engine observation frame")

    def _finish_group(self):
        identity, protocol, words, observed = self.group
        self.group = None
        if self.job.code == "blockcheck2":
            pending = self.pending.setdefault(identity, {})
            pending.update(observed)
            if len(pending) != len(self.job.domains):
                return
            observed = self.pending.pop(identity)
        if set(observed) != set(self.job.domains):
            raise ValueError("Engine did not check all selected domains")
        files = {}
        if any(observed.values()):
            for word in words:
                reference = _file_argument(word)
                if reference is not None:
                    name = reference[1]
                    path = (self.base / name).resolve(strict=True)
                    if not path.is_relative_to(self.base) or not path.is_file():
                        raise ValueError("Strategy dependency is outside the installed engine")
                    with path.open("rb") as file:
                        contents = file.read(16 * 1024 * 1024 + 1)
                    if len(contents) > 16 * 1024 * 1024:
                        raise ValueError("Strategy dependency exceeds the capture limit")
                    files[name] = contents
        self.job.check(identity, protocol, shlex.join(words), files, observed)


class BlockcheckEngine:
    """Resolve only the installed, managed engine; never a request-supplied command."""

    def prepare(self, code, parameters):
        blockcheck = shutil.which("blockcheck2.sh")
        helper = root_helper_status()
        if not blockcheck or not helper["ready"]:
            raise RuntimeError("Managed blockcheck2 is not ready")
        target = _resolve_blockcheck_script(Path(blockcheck))
        helper_path = Path(str(helper["path"]))
        if b"GP_API2_TRACE" not in helper_path.read_bytes():
            raise RuntimeError("Installed helper does not support API 2")
        base = target.parent
        scripts = sorted((base / "blockcheck2.d" / "standard").glob("*.sh"))
        if not scripts or not shutil.which("nfqws2") or not shutil.which("curl"):
            raise RuntimeError("Managed blockcheck2 runtime is incomplete")
        files = [target, helper_path, *scripts, *sorted((base / "lua").glob("*.lua"))]
        digest = sha256()
        for path in files:
            with path.open("rb") as file:
                digest.update(file_digest(file, "sha256").digest())
        return digest.hexdigest()

    def run(self, job):
        if job.cancel.is_set():
            return "stopped"
        target = _resolve_blockcheck_script(Path(shutil.which("blockcheck2.sh")))
        base = target.parent
        params = dict(job.parameters)
        debug = params.pop("debug_stdout")
        parallel = params.pop("curl_parallelism", 4)
        params["enable_quic"] = params.pop("include_quic")
        options = DiscoveryOptions(**params)
        marker = uuid4().hex
        env = {
            **os.environ, **options.to_blockcheck_env(validated=True), "BATCH": "1", "TEST": "standard",
            "DOMAINS": " ".join(job.domains), "IPVS": _ipvs_value(options),
            "ZAPRET_BASE": str(base), "ZAPRET_RW": str(base), "GP_API2_TRACE": marker,
            "GP_MD_CURL_PARALLELISM": str(parallel),
            "GP_DEBUG_STDOUT": "1" if debug else "0",
        }
        command = root_command(
            [str(target)], env=env, pass_env_keys=(*BLOCKCHECK_ENV_KEYS, "GP_API2_TRACE"),
            helper_command="run-multidomain" if job.checkpoint is not None else "run",
            run_id=job.run_id,
        )
        observer = _Observations(job, base, marker)
        log = None
        if debug:
            logs = job.state_dir / "strategy-finder" / "diagnostics"
            logs.mkdir(mode=0o700, parents=True, exist_ok=True)
            log = logs / (job.run_id + ".log")
        with TemporaryFile(mode="w+b") as replay:
            if job.checkpoint is not None:
                for identity, success in job.checkpoint["trail"]:
                    replay.write(f"{identity} {int(success)}\n".encode("ascii"))
                replay.write(b"end\n")
            replay.seek(0)
            result = _run_process_with_live_stdout(
                command, env, None, Path(os.devnull), log,
                0, job.cancel, observer, job.run_id, stdin=replay, stream_stderr=True,
            )
        if result["stopped"]:
            return "stopped"
        return "completed" if result["returncode"] == 0 and observer.group is None else "failed"
