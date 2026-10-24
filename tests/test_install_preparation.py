"""Execute installer ordering with isolated command controls, never host services."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or (r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else None)


def shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value


class PreparedPackageVersionTests(unittest.TestCase):
    def test_installer_executes_source_package_and_metadata_version_check(self):
        script = (ROOT / "scripts/install-linux.sh").read_text(encoding="utf-8")
        lines = [line for line in script.splitlines() if line.startswith("runuser ") and " -c " in line]
        self.assertEqual(len(lines), 1)
        arguments = shlex.split(lines[0])
        code = arguments[arguments.index("-c") + 1]
        self.assertEqual(arguments[-1], "$SOURCE_DIR")
        self.assertLess(script.index(lines[0]), script.index("phase=activation"))
        for source, package, metadata, docs, success in (
            ("0.4.3", "0.4.3", "0.4.3", True, True),
            ("0.4.4", "0.4.4", "0.4.4", True, True),
            ("0.4.5", "0.4.5", "0.4.5", True, True),
            ("0.4.4", "0.4.3", "0.4.4", True, False),
            ("0.4.4", "0.4.4", "0.4.3", True, False),
            ("0.4.4", "0.4.4", "0.4.4", False, False),
        ):
            with self.subTest(source=source, package=package, metadata=metadata, docs=docs), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "pyproject.toml").write_text(f'[project]\nversion = "{source}"\n', encoding="utf-8")
                (root / "bottle.py").write_text("", encoding="utf-8")
                (root / "cheroot.py").write_text("", encoding="utf-8")
                web = root / "gp_control_plane/web"
                web.mkdir(parents=True)
                (web.parent / "__init__.py").write_text(f'__version__ = "{package}"\n', encoding="utf-8")
                (web / "__init__.py").write_text("", encoding="utf-8")
                (web / "docs.py").write_text(f'def openapi_json_bytes():\n    return {b"{}" if docs else b""!r}\n', encoding="utf-8")
                dist = root / "gp_access_control_plane.dist-info"
                dist.mkdir()
                (dist / "METADATA").write_text(f'Name: gp-access-control-plane\nVersion: {metadata}\n', encoding="utf-8")
                result = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); " + code, str(root)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if not success:
                    self.assertIn("ERROR: prepared GP", result.stderr)


@unittest.skipUnless(BASH, "bash is required")
class InstallPreparationTests(unittest.TestCase):
    def test_preparation_failures_preserve_previous_installation(self):
        # Command controls simulate failure boundaries; real archive/runtime
        # correctness is covered separately by test_zapret2_release and Linux gates.
        control = r'''
set -Eeuo pipefail
event() { printf '%s\n' "$*" >> "$TEST_EVENTS"; }
id() { case "$1" in -u) echo 0;; -gn) echo gpuser;; *) return 64;; esac; }
getent() { printf 'gpuser:x:1000:1000::%s:/bin/bash\n' "$TEST_HOME"; }
git() { case "$3" in rev-parse) printf '%040d\n' 1;; status) :;; *) return 64;; esac; }
uname() { case "$1" in -s) echo Linux;; -m) echo "${TEST_MACHINE:-aarch64}";; esac; }
apt-get() { event "apt:$*"; [ "$TEST_FAIL" != apt ]; }
mktemp() { command mktemp -d "$TEST_TMP/preparation.XXXXXX"; }
chmod() { :; }
install() {
  event "install:$*"
  local target=${!#}
  case "$target" in "$TEST_TMP"/preparation.*/*)
    if [ "$1" = -d ]; then command mkdir -p -- "$target";
    elif [ "$1" = -m ] && [ "$2" = 0644 ]; then
      if [ "$TEST_FAIL" = lock-copy ]; then printf 'corrupt staged lock\n' > "$target";
      else command cp -- "$3" "$4"; fi
    else return 64; fi;;
  esac
}
bash() {
  event zapret-prepare
  case "$TEST_FAIL" in network|checksum|archive|architecture)
    printf 'ERROR: zapret2 %s failed\n' "$TEST_FAIL" >&2; return 61;; esac
  return 0
}
runuser() {
  event "user:$*"
  case "$*" in
    *" --verify "*) return 0;;
    *" --probe "*) [ "$TEST_FAIL" != runtime ]; return;;
    *" -m venv "*) [ "$TEST_FAIL" != venv ]; return;;
    *" -m pip download "*) case "$TEST_FAIL" in dependencies|dependency-checksum|dependency-missing) return 65;; *) return 0;; esac;;
    *" -m pip wheel "*) [ "$TEST_FAIL" != wheel ]; return;;
    *" -m pip install "*) [ "$TEST_FAIL" != package ]; return;;
    *" -m pip check"*) [ "$TEST_FAIL" != dependency-check ]; return;;
    *" -c "*)
      if [ "$TEST_FAIL" = import ]; then return 62; fi
      # Successful preparation stops here, before any /opt or activation work.
      event PREPARATION_COMPLETE; return 93;;
    *) return 64;;
  esac
}
systemctl() { event "FORBIDDEN-systemctl:$*"; return 99; }
rm() {
  event "rm:$*"
  case "$*" in *"$TEST_TMP/preparation."*) command rm "$@";;
    *) event FORBIDDEN-removal; return 99;; esac
}
source "$1" --source-dir "$TEST_SOURCE" --install-user gpuser --candidate-sha 0000000000000000000000000000000000000001 --web off --initial-install off
'''
        for failure in ("apt", "network", "checksum", "archive", "architecture", "runtime", "venv", "lock-copy", "dependencies", "dependency-checksum", "dependency-missing", "dependency-check", "wheel", "package", "import", "none"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw); home = root / "home"; source = root / "source"
                old = home / "gp/GP-access-control-plane"; state = home / "gp/.GP-access-control-plane.data/state"
                old.mkdir(parents=True); state.mkdir(parents=True)
                (old / "settings.ini").write_bytes(b"existing settings")
                (state / "user-data").write_bytes(b"existing user data")
                (source / ".git").mkdir(parents=True); (source / "scripts").mkdir()
                (source / "scripts/clean-install-vault.py").write_text("# fixture", encoding="utf-8")
                (source / "requirements").mkdir()
                shutil.copyfile(ROOT / "requirements/a5-transport.lock", source / "requirements/a5-transport.lock")
                events = root / "events"
                env = {**os.environ, "TEST_EVENTS": shell_path(events), "TEST_HOME": shell_path(home),
                       "TEST_SOURCE": shell_path(source), "TEST_TMP": shell_path(root), "TEST_FAIL": failure}
                result = subprocess.run([BASH, "--noprofile", "--norc", "-c", control, "bash",
                                         shell_path(ROOT / "scripts/install-linux.sh")],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("ERROR:", result.stderr)
                trace = events.read_text(encoding="utf-8")
                self.assertNotIn("FORBIDDEN", trace, trace)
                self.assertEqual((old / "settings.ini").read_bytes(), b"existing settings")
                self.assertEqual((state / "user-data").read_bytes(), b"existing user data")
                self.assertFalse(list(root.glob("preparation.*")), "owned staging must be cleaned")
                if failure == "none":
                    self.assertIn("PREPARATION_COMPLETE", trace, result.stderr)
                    self.assertIn("--no-index", trace)
                    self.assertIn("--require-hashes", trace)
                    self.assertIn("--no-deps", trace)
                else:
                    self.assertNotIn("PREPARATION_COMPLETE", trace)


if __name__ == "__main__":
    unittest.main()
