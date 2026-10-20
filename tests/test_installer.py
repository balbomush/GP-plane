from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import os
from pathlib import Path


class CleanInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.bootstrap = (root / "scripts" / "bootstrap-linux.sh").read_text(encoding="utf-8")
        cls.hardware_bootstrap = (root / "scripts" / "hardware-candidate-bootstrap.sh").read_text(encoding="utf-8")
        cls.installer = (root / "scripts" / "install-linux.sh").read_text(encoding="utf-8")

    def test_user_flow_accepts_only_exact_annotated_tag_and_one_sudo(self) -> None:
        self.assertIn('TAG="${GP_BRANCH:-}"', self.bootstrap)
        self.assertIn("exact annotated stable or alpha release tag", self.bootstrap)
        self.assertIn("^v[0-9]+\\.[0-9]+\\.[0-9]+(-alpha\\.[1-9][0-9]*)?$", self.bootstrap)
        self.assertIn('cat-file -t "refs/tags/$TAG"', self.bootstrap)
        self.assertIn('python3 "$source_dir/scripts/clean-install-vault.py"', self.bootstrap)
        self.assertEqual(self.bootstrap.count("sudo --"), 1)
        self.assertIn('git -C "$source_dir" status --porcelain', self.bootstrap)
        self.assertIn('v040_checkout_state="$HOME/gp/GP-access-control-plane/build/state"', self.bootstrap)
        self.assertIn('v040_data_state="$HOME/gp/.GP-access-control-plane.data/state"', self.bootstrap)
        self.assertIn('both supported v0.4 state sources exist', self.bootstrap)
        self.assertIn('canonical v0.4 state is not a non-symlink directory', self.bootstrap)
        self.assertIn('canonical v0.4 state has an invalid layout', self.bootstrap)
        self.assertIn('canonical v0.4 strategy-finder is not a non-symlink directory', self.bootstrap)
        self.assertIn('canonical v0.4 strategy-finder path escapes state', self.bootstrap)
        self.assertIn('verify_vault() {', self.bootstrap)
        self.assertIn('--verify --state-dir "$1" --home "$HOME"', self.bootstrap)
        self.assertIn('if verify_vault "$v040_state" 2>/dev/null; then', self.bootstrap)
        self.assertIn('elif verify_vault "$v040_data_state" 2>/dev/null; then', self.bootstrap)
        self.assertIn('verify_vault "$v040_state"\n', self.bootstrap)
        self.assertIn('initial_install=on', self.bootstrap)
        self.assertIn('--initial-install "$initial_install"', self.bootstrap)
        for forbidden in ("latest-stable", "refs/heads", "GP_EXPECTED_SHA", "candidate", "rollback", "clean-remove"):
            self.assertNotIn(forbidden, self.bootstrap)

    def test_bootstrap_accepts_only_stable_or_positive_alpha_tags_before_python_or_sudo(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw); fake_bin = sandbox / "bin"; fake_bin.mkdir(); log = sandbox / "calls.log"

            def bash_path(path: Path) -> str:
                value = path.resolve().as_posix()
                return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value

            def fake(name: str, body: str) -> None:
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
                path.chmod(0o755)

            fake("id", 'case "$1" in -u) echo 1000;; -un) echo gpuser;; *) exit 64;; esac\n')
            fake("git", 'echo GIT >> "$TEST_LOG"\ncase "$1" in clone) dest="${!#}"; mkdir -p "$dest/scripts";; -C) shift 2; case "$1" in cat-file) echo "${GP_TEST_TAG_TYPE:-tag}";; checkout|status) :;; rev-parse) echo deadbeef;; *) exit 64;; esac;; *) exit 64;; esac\n')
            fake("python3", 'printf "PYTHON:%s\\n" "$*" >> "$TEST_LOG"\nexit 42\n')
            fake("sudo", 'echo SUDO >> "$TEST_LOG"\nexit 42\n')
            invoke = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2"', "bash", bash_path(fake_bin), str(root / "scripts" / "bootstrap-linux.sh")]
            for tag in ("v0.4.1", "v0.4.1-alpha.1", "v12.34.56-alpha.999"):
                with self.subTest(accepted=tag):
                    if log.exists(): log.unlink()
                    result = subprocess.run(invoke, env={**os.environ, "HOME": bash_path(sandbox / "home"), "GP_BRANCH": tag, "TEST_LOG": bash_path(log)}, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    calls = log.read_text(encoding="utf-8").splitlines()
                    self.assertIn("GIT", calls)
                    python_calls = [call for call in calls if call.startswith("PYTHON:")]
                    self.assertEqual(len(python_calls), 1, calls)
                    self.assertIn("--verify --state-dir", python_calls[0])
                    self.assertIn(
                        f"--state-dir {bash_path(sandbox / 'home' / 'gp' / '.GP-access-control-plane.data' / 'state')}",
                        python_calls[0],
                    )
                    self.assertEqual(calls[-1], "SUDO")
            for tag in ("v0.4.1-alpha.0", "v0.4.1-alpha.-1", "v0.4.1-alpha.01", "v0.4.1-beta.1", "v0.4.1-rc.1", "main", "v0.4.1^{commit}"):
                with self.subTest(rejected=tag):
                    if log.exists(): log.unlink()
                    result = subprocess.run(invoke, env={**os.environ, "HOME": bash_path(sandbox / "home"), "GP_BRANCH": tag, "TEST_LOG": bash_path(log)}, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("exact release tag", result.stderr)
                    self.assertFalse(log.exists(), "malformed tag must stop before git, Python, sudo, or removal")
            if log.exists(): log.unlink()
            lightweight = subprocess.run(
                invoke,
                env={**os.environ, "HOME": bash_path(sandbox / "home"), "GP_BRANCH": "v0.4.1-alpha.1", "GP_TEST_TAG_TYPE": "commit", "TEST_LOG": bash_path(log)},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(lightweight.returncode, 0)
            self.assertIn("annotated", lightweight.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["GIT", "GIT"])

    def test_internal_hardware_transport_accepts_only_frozen_dev_commit(self) -> None:
        self.assertIn("--candidate-sha <40-lowercase-hex>", self.hardware_bootstrap)
        self.assertIn("git clone --no-checkout --depth=1 --branch dev", self.hardware_bootstrap)
        self.assertIn('rev-parse refs/remotes/origin/dev', self.hardware_bootstrap)
        self.assertIn('^[0-9a-f]{40}$', self.hardware_bootstrap)
        self.assertEqual(self.hardware_bootstrap.count("sudo -n --"), 1)
        self.assertIn('--candidate-sha "$CANDIDATE_SHA"', self.hardware_bootstrap)
        self.assertNotIn("GP_BRANCH", self.hardware_bootstrap)
        self.assertNotIn("GP_REPO_URL", self.hardware_bootstrap)
        self.assertNotIn("hardware-candidate-bootstrap", (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8"))

    def test_both_bootstraps_support_exactly_one_v040_state_source(self) -> None:
        for bootstrap in (self.bootstrap, self.hardware_bootstrap):
            self.assertIn('v040_checkout_state="$HOME/gp/GP-access-control-plane/build/state"', bootstrap)
            self.assertIn('v040_data_state="$HOME/gp/.GP-access-control-plane.data/state"', bootstrap)
            self.assertIn('--state-dir "$v040_state" --home "$HOME"', bootstrap)
            self.assertIn('--verify --state-dir "$1" --home "$HOME"', bootstrap)
            self.assertIn('if verify_vault "$v040_state" 2>/dev/null; then', bootstrap)
            self.assertIn('elif verify_vault "$v040_data_state" 2>/dev/null; then', bootstrap)
            self.assertIn('verify_vault "$v040_state"\n', bootstrap)
            self.assertIn('both supported v0.4 state sources exist', bootstrap)

    def test_root_process_verifies_vault_before_fixed_removal_and_installs_both_topologies(self) -> None:
        verify = self.installer.index('runuser -u "$INSTALL_USER" -- python3 "$vault_tool" --verify')
        removal = self.installer.index('rm -rf --one-file-system -- /usr/local/libexec/gp-control-plane')
        restore = self.installer.index('"$install_dir/.venv/bin/python" "$vault_tool" --restore --target-state-dir "$state_dir" --home "$target_home"')
        prepare = self.installer.index('domain-sources prepare-v2fly')
        first_service_start = self.installer.index('systemctl daemon-reload; systemctl enable --now gp-control-plane-core.service')
        self.assertLess(verify, removal)
        self.assertLess(removal, restore)
        self.assertLess(restore, prepare)
        self.assertLess(restore, first_service_start)
        restore_guard = self.installer.index('# The application validates the pending vault ID')
        restore_guard_end = self.installer.index('\nfi\n', restore_guard)
        self.assertIn('if [ "$INITIAL_INSTALL" = off ]; then', self.installer[restore_guard:restore_guard_end])
        self.assertIn('--restore --target-state-dir "$state_dir"', self.installer[restore_guard:restore_guard_end])
        self.assertNotIn('systemctl enable --now', self.installer[restore_guard:restore_guard_end])
        self.assertIn('gp-control-plane-core.service', self.installer)
        self.assertIn('[ "$INSTALL_WEB" = off ] || fail', self.installer)
        self.assertNotIn('cat > /etc/systemd/system/gp-control-plane-web.service', self.installer)
        self.assertIn('git -C "$SOURCE_DIR" status --porcelain', self.installer)
        self.assertIn('managed GP root is not canonical', self.installer)
        self.assertIn('gp-control-plane-root-helper', self.installer)
        self.assertIn('case "$INITIAL_INSTALL" in on|off)', self.installer)
        self.assertIn('if [ "$INITIAL_INSTALL" = off ]; then', self.installer)
        self.assertIn('python3 "$vault_tool" --verify --home "$target_home"', self.installer)
        self.assertEqual(self.installer.count('--restore --target-state-dir "$state_dir"'), 1)
        self.assertIn('visudo -cf /etc/sudoers.d/gp-control-plane-root-helper', self.installer)
        self.assertIn('scripts/install-zapret2.sh', self.installer)
        zapret_prepare = self.installer.index('bash "$SOURCE_DIR/scripts/install-zapret2.sh" --prepare-dir')
        runtime_probe = self.installer.index('--probe --destination "$prepare_dir/runtime"')
        package_probe = self.installer.index('openapi_json_bytes() or sys.exit("ERROR: prepared GP OpenAPI document is empty")')
        service_stop = self.installer.index('stop_unit gp-control-plane-web.service')
        self.assertLess(verify, zapret_prepare)
        self.assertLess(zapret_prepare, runtime_probe)
        self.assertLess(runtime_probe, package_probe)
        self.assertLess(package_probe, service_stop)
        self.assertLess(service_stop, removal)
        self.assertIn('/usr/local/libexec/gp-control-plane/nfqws2', self.installer)
        self.assertIn('/usr/local/libexec/gp-control-plane/blockcheck2.sh', self.installer)
        self.assertIn('Environment=PATH=/usr/local/libexec/gp-control-plane:', self.installer)
        self.assertNotIn('/usr/local/bin/nfqws2', self.installer)
        self.assertNotIn('/usr/local/bin/blockcheck2.sh', self.installer)
        self.assertNotIn('rm -rf --one-file-system -- /usr/local/bin', self.installer)
        self.assertNotIn('rm -f -- /usr/local/bin', self.installer)
        self.assertNotIn("\nsudo ", self.installer)
        self.assertIn("--candidate-sha", self.installer)
        self.assertIn("candidate SHA must be a full lowercase commit SHA", self.installer)
        self.assertIn("source checkout does not match the exact candidate SHA", self.installer)
        for forbidden in ("adapter", "provision", "preflight", "manifest", "rollback", "snapshot"):
            self.assertNotIn(forbidden, self.installer)

    def test_installer_persists_only_the_validated_tag_or_candidate_identity(self) -> None:
        self.assertIn('SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"', self.installer)
        self.assertIn(
            "printf '%s\\n' \"$SOURCE_COMMIT\" | grep -Eq '^[0-9a-f]{40}$' || fail 'source checkout has an invalid HEAD commit'",
            self.installer,
        )
        self.assertIn('[ "$SOURCE_COMMIT" = "$(git -C "$SOURCE_DIR" rev-parse "refs/tags/$TAG^{commit}")" ]', self.installer)
        self.assertIn('INSTALL_REF="$TAG"', self.installer)
        self.assertIn('[ "$SOURCE_COMMIT" = "$CANDIDATE_SHA" ]', self.installer)
        self.assertIn('INSTALL_REF="candidate:$CANDIDATE_SHA"', self.installer)
        self.assertLess(
            self.installer.index('[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]'),
            self.installer.index('INSTALL_COMMIT="$SOURCE_COMMIT"'),
        )

        for service in ("core",):
            default_file = f"/etc/default/gp-control-plane-{service}"
            start = self.installer.index(f"cat > {default_file} <<EOF")
            end = self.installer.index("\nEOF\n", start)
            contents = self.installer[start:end]
            self.assertIn("GP_INSTALLED_REF='$INSTALL_REF'", contents)
            self.assertIn("GP_INSTALLED_COMMIT='$INSTALL_COMMIT'", contents)

        self.assertNotIn('cat > /etc/default/gp-control-plane-web <<EOF', self.installer)

        self.assertNotIn("GP_INSTALLED_REF='$CANDIDATE_SHA'", self.installer)
        self.assertNotIn("GP_INSTALLED_COMMIT='$TAG'", self.installer)

    def test_state_hierarchy_is_private_and_owned_by_the_install_user(self) -> None:
        self.assertIn('state_parent="$gp_root/.GP-access-control-plane.data"', self.installer)
        self.assertIn('state_dir="$state_parent/state"', self.installer)
        self.assertIn('"$install_dir" "$state_parent"', self.installer)
        self.assertIn(
            'install -d -m 0700 -o "$INSTALL_USER" -g "$group" "$state_parent" "$state_dir"',
            self.installer,
        )

    def test_installer_prepares_v2fly_once_as_install_user_without_blocking_service_start(self) -> None:
        prepare = 'runuser -u "$INSTALL_USER" -- env GP_STATE_DIR="$state_dir" "$install_dir/.venv/bin/gp-control-plane" domain-sources prepare-v2fly'
        pip_install = 'runuser -u "$INSTALL_USER" -- "$install_dir/.venv/bin/python" -m pip install --no-index --find-links "$prepare_dir/python/wheels" gp-access-control-plane'
        first_service_start = 'systemctl daemon-reload; systemctl enable --now gp-control-plane-core.service'

        self.assertEqual(self.installer.count(prepare), 1)
        self.assertLess(self.installer.index(pip_install), self.installer.index(prepare))
        self.assertLess(self.installer.index('--restore --target-state-dir "$state_dir"'), self.installer.index(prepare))
        self.assertLess(self.installer.index(prepare), self.installer.index(first_service_start))
        self.assertIn(f'if ! {prepare}; then', self.installer)
        self.assertIn('WARNING: v2fly catalog was not prepared; start the service and retry from the Web interface.', self.installer)
        prepare_block_start = self.installer.index(f'if ! {prepare}; then')
        prepare_block_end = self.installer.index('\nfi\n', prepare_block_start)
        self.assertNotIn('sudo', self.installer[prepare_block_start:prepare_block_end])

    def test_v2fly_prepare_failure_is_best_effort_and_reaches_the_core_service_start(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        start = self.installer.index("# The v2fly cache is disposable service data.")
        end = self.installer.index("install -d -m 0755 /usr/local/libexec/gp-control-plane", start)
        v2fly_block = self.installer[start:end]
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw)
            fake_bin = sandbox / "bin"
            fake_bin.mkdir()
            log = sandbox / "calls.log"

            def bash_path(path: Path) -> str:
                value = path.resolve().as_posix()
                return f"/{value[0].lower()}{value[2:]}" if len(value) > 2 and value[1] == ":" else value

            for name, body in {
                "runuser": 'printf "runuser:%s\\n" "$*" >> "$TEST_LOG"\nexit 42\n',
                "systemctl": 'printf "systemctl:%s\\n" "$*" >> "$TEST_LOG"\n',
            }.items():
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
                path.chmod(0o755)
            result = subprocess.run(
                [
                    bash,
                    "--noprofile",
                    "--norc",
                    "-c",
                    'PATH="$1:/usr/bin:/bin"; export PATH; exec bash -c "$2"',
                    "bash",
                    bash_path(fake_bin),
                    v2fly_block + 'systemctl enable --now gp-control-plane-core.service\n',
                ],
                env={
                    **os.environ,
                    "TEST_LOG": bash_path(log),
                    "INSTALL_USER": "gpuser",
                    "state_dir": bash_path(sandbox / "state"),
                    "install_dir": bash_path(sandbox / "install"),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WARNING: v2fly catalog was not prepared", result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines()[-1], "systemctl:enable --now gp-control-plane-core.service")
            self.assertIn("GP_STATE_DIR=", log.read_text(encoding="utf-8"))

    def test_retired_transition_entrypoints_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("clean-install-vault.sh", "gp-clean-remove-root.sh", "gp-clean-remove-preflight.sh", "gp-clean-remove-provision-root.sh", "legacy-bootstrap.sh", "legacy-bootstrap-launcher.sh"):
            self.assertFalse((root / name).exists(), name)

    def test_shell_syntax(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        for script in ("bootstrap-linux.sh", "hardware-candidate-bootstrap.sh", "install-linux.sh"):
            result = subprocess.run([bash, "-n", str(root / "scripts" / script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_gp_owned_zapret_wrapper_is_discoverable_in_service_path_model(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is required")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); libexec = root / "usr" / "local" / "libexec" / "gp-control-plane"; zapret = root / "opt" / "zapret2"
            libexec.mkdir(parents=True); zapret.mkdir(parents=True)
            target = zapret / "nfqws2"; target.write_text("#!/bin/sh\nprintf 'ready:%s\\n' \"$1\"\n", encoding="utf-8"); target.chmod(0o755)
            wrapper = libexec / "nfqws2"; wrapper.write_text(f"#!/bin/sh\nexec '{target.as_posix()}' \"$@\"\n", encoding="utf-8"); wrapper.chmod(0o755)
            result = subprocess.run([bash, "-c", "command -v nfqws2; nfqws2 probe"], env={"PATH": str(libexec)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(wrapper), result.stdout)
            self.assertIn("ready:probe", result.stdout)

    def test_each_single_v040_source_creates_its_vault_and_rejects_a_stale_retry(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw); fake_bin = sandbox / "bin"
            fake_bin.mkdir()
            def bash_path(path: Path) -> str:
                raw_path = path.resolve().as_posix()
                return f"/{raw_path[0].lower()}{raw_path[2:]}" if len(raw_path) > 2 and raw_path[1] == ":" else raw_path
            def fake(name: str, body: str) -> None:
                path = fake_bin / name; path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8"); path.chmod(0o755)
            fake("id", 'case "$1" in -u) echo 1000;; -un) echo gpuser;; *) exit 64;; esac\n')
            fake("git", 'case "$1" in clone) dest="${!#}"; mkdir -p "$dest/scripts";; -C) shift 2; case "$1" in cat-file) echo tag;; rev-parse) printf "%s\\n" "${GP_TEST_CANDIDATE_SHA:-deadbeef}";; checkout) :;; status) :;; *) exit 64;; esac;; *) exit 64;; esac\n')
            fake("python3", 'case " $* " in *" --verify "*) printf "VERIFY:%s\\n" "$*" >> "$TEST_LOG"; [ -e "$VAULT_MARKER" ];; *) printf "CREATE:%s\\n" "$*" >> "$TEST_LOG"; : > "$VAULT_MARKER";; esac\n')
            fake("sudo", 'printf "SUDO:%s\\n" "$*" >> "$TEST_LOG"; exit "${SUDO_RESULT:-73}"\n')
            for source_kind, source_relative in (
                ("checkout", Path("gp") / "GP-access-control-plane" / "build" / "state"),
                ("data", Path("gp") / ".GP-access-control-plane.data" / "state"),
            ):
                for script in ("bootstrap-linux.sh", "hardware-candidate-bootstrap.sh"):
                    with self.subTest(source_kind=source_kind, script=script):
                        home = sandbox / f"home-{source_kind}-{script}"
                        log = sandbox / f"{source_kind}-{script}.log"
                        vault_marker = sandbox / f"{source_kind}-{script}.vault-ready"
                        v040_state = home / source_relative
                        (v040_state / "strategy-finder").mkdir(parents=True)
                        (v040_state / "strategy-finder" / "state.sqlite3").write_bytes(b"sqlite")
                        env = {**os.environ, "HOME": bash_path(home), "TEST_LOG": bash_path(log), "VAULT_MARKER": bash_path(vault_marker), "GP_TEST_CANDIDATE_SHA": "a" * 40}
                        if script == "bootstrap-linux.sh":
                            env["GP_BRANCH"] = "v0.4.0"
                            invoke = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2"', "bash", bash_path(fake_bin), str(root / "scripts" / script)]
                        else:
                            invoke = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2" --candidate-sha "$3"', "bash", bash_path(fake_bin), str(root / "scripts" / script), "a" * 40]
                        first = subprocess.run(invoke, env={**env, "SUDO_RESULT": "73"}, capture_output=True, text=True)
                        self.assertEqual(first.returncode, 73, first.stderr)
                        (v040_state / "strategy-finder" / "state.sqlite3").write_bytes(b"newer source data")
                        second = subprocess.run(invoke, env={**env, "SUDO_RESULT": "0"}, capture_output=True, text=True)
                        self.assertNotEqual(second.returncode, 0)
                        self.assertIn("pending clean-install vault exists while canonical v0.4 state is still live", second.stderr)
                        calls = log.read_text(encoding="utf-8").splitlines()
                        self.assertEqual(len(calls), 5, calls)
                        self.assertEqual(sum(call.startswith("CREATE:") for call in calls), 1, calls)
                        self.assertEqual(sum(call.startswith("SUDO:") for call in calls), 1, calls)
                        self.assertIn(f"--state-dir {bash_path(v040_state)}", next(call for call in calls if call.startswith("CREATE:")))
                        self.assertTrue(all(f"--state-dir {bash_path(v040_state)}" in call for call in calls if call.startswith("VERIFY:")), calls)
                        self.assertTrue(all("--initial-install off" in call for call in calls if call.startswith("SUDO:")), calls)

    def test_unsafe_or_ambiguous_v040_state_stops_before_sudo_for_both_bootstraps(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        candidate = "a" * 40
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw)
            fake_bin = sandbox / "bin"
            fake_bin.mkdir()

            def bash_path(path: Path) -> str:
                raw_path = path.resolve().as_posix()
                return f"/{raw_path[0].lower()}{raw_path[2:]}" if len(raw_path) > 2 and raw_path[1] == ":" else raw_path

            def fake(name: str, body: str) -> None:
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
                path.chmod(0o755)

            fake("id", 'case "$1" in -u) echo 1000;; -un) echo gpuser;; *) exit 64;; esac\n')
            fake("git", 'case "$1" in clone) dest="${!#}"; mkdir -p "$dest/scripts";; -C) shift 2; case "$1" in cat-file) echo tag;; checkout|status) :;; rev-parse) printf "%s\\n" "${GP_TEST_CANDIDATE_SHA}";; *) exit 64;; esac;; *) exit 64;; esac\n')
            fake("python3", 'echo PYTHON >> "$TEST_LOG"; exit 99\n')
            fake("sudo", 'echo SUDO >> "$TEST_LOG"; exit 64\n')

            for kind in ("symlink", "file", "missing-strategy-finder", "missing-database", "strategy-finder-symlink", "database-symlink", "both-sources"):
                for script in ("bootstrap-linux.sh", "hardware-candidate-bootstrap.sh"):
                    with self.subTest(kind=kind, script=script):
                        home = sandbox / f"home-{kind}-{script}"
                        v040_state = home / "gp" / ".GP-access-control-plane.data" / "state"
                        v040_state.parent.mkdir(parents=True)
                        if kind == "symlink":
                            target = sandbox / f"target-{script}"
                            target.mkdir()
                            try:
                                v040_state.symlink_to(target, target_is_directory=True)
                            except OSError as exc:
                                self.skipTest(f"symlink creation is unavailable: {exc}")
                        elif kind == "file":
                            v040_state.write_text("not a directory", encoding="utf-8")
                        elif kind == "missing-strategy-finder":
                            v040_state.mkdir()
                        elif kind == "missing-database":
                            (v040_state / "strategy-finder").mkdir(parents=True)
                        elif kind == "strategy-finder-symlink":
                            v040_state.mkdir()
                            target = sandbox / f"strategy-target-{script}"
                            target.mkdir()
                            (target / "state.sqlite3").write_bytes(b"sqlite")
                            try:
                                (v040_state / "strategy-finder").symlink_to(target, target_is_directory=True)
                            except OSError as exc:
                                self.skipTest(f"symlink creation is unavailable: {exc}")
                        else:
                            if kind == "database-symlink":
                                (v040_state / "strategy-finder").mkdir(parents=True)
                                target = sandbox / f"database-target-{script}"
                                target.write_bytes(b"sqlite")
                                try:
                                    (v040_state / "strategy-finder" / "state.sqlite3").symlink_to(target)
                                except OSError as exc:
                                    self.skipTest(f"symlink creation is unavailable: {exc}")
                            else:
                                checkout_state = home / "gp" / "GP-access-control-plane" / "build" / "state"
                                for state in (checkout_state, v040_state):
                                    (state / "strategy-finder").mkdir(parents=True)
                                    (state / "strategy-finder" / "state.sqlite3").write_bytes(b"sqlite")
                        log = sandbox / f"{kind}-{script}.log"
                        env = {**os.environ, "HOME": bash_path(home), "TEST_LOG": bash_path(log), "GP_TEST_CANDIDATE_SHA": candidate}
                        if script == "bootstrap-linux.sh":
                            env["GP_BRANCH"] = "v0.4.0"
                            command = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2"', "bash", bash_path(fake_bin), str(root / "scripts" / script)]
                        else:
                            command = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2" --candidate-sha "$3"', "bash", bash_path(fake_bin), str(root / "scripts" / script), candidate]
                        completed = subprocess.run(command, env=env, capture_output=True, text=True)
                        self.assertNotEqual(completed.returncode, 0)
                        expected_error = "both supported v0.4 state sources exist" if kind == "both-sources" else "canonical v0.4"
                        self.assertIn(expected_error, completed.stderr)
                        self.assertFalse(log.exists(), "unsafe or ambiguous v0.4 state must stop before Python or sudo")

    def test_hardware_bootstrap_rejects_non_frozen_or_short_sha_before_sudo(self) -> None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = shutil.which("bash") or (str(git_bash) if git_bash.is_file() else None)
        if not bash:
            self.skipTest("bash is required")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as raw:
            sandbox = Path(raw); fake_bin = sandbox / "bin"; home = sandbox / "home"; log = sandbox / "calls.log"
            fake_bin.mkdir(); home.mkdir()
            def bash_path(path: Path) -> str:
                raw_path = path.resolve().as_posix()
                return f"/{raw_path[0].lower()}{raw_path[2:]}" if len(raw_path) > 2 and raw_path[1] == ":" else raw_path
            def fake(name: str, body: str) -> None:
                path = fake_bin / name; path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8"); path.chmod(0o755)
            fake("id", 'case "$1" in -u) echo 1000;; -un) echo gpuser;; *) exit 64;; esac\n')
            fake("git", 'case "$1" in clone) dest="${!#}"; mkdir -p "$dest/scripts";; -C) shift 2; case "$1" in checkout|status) :;; rev-parse) printf "%s\\n" "${GP_TEST_CANDIDATE_SHA}";; *) exit 64;; esac;; *) exit 64;; esac\n')
            fake("python3", 'exit 0\n')
            fake("sudo", '[ "$1" = -n ] && [ "$2" = -- ] || exit 64\necho SUDO >> "$TEST_LOG"\n')
            candidate = "a" * 40
            invoke = [bash, "--noprofile", "--norc", "-c", 'PATH="$1:/usr/bin:/bin"; export PATH; exec "$2" --candidate-sha "$3"', "bash", bash_path(fake_bin), str(root / "scripts" / "hardware-candidate-bootstrap.sh")]
            env = {**os.environ, "HOME": bash_path(home), "TEST_LOG": bash_path(log), "GP_TEST_CANDIDATE_SHA": candidate}
            rejected = subprocess.run([*invoke, "deadbeef"], env=env, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(log.exists())
            not_frozen = subprocess.run([*invoke, "b" * 40], env=env, capture_output=True, text=True)
            self.assertNotEqual(not_frozen.returncode, 0)
            self.assertFalse(log.exists())
            accepted = subprocess.run([*invoke, candidate], env=env, capture_output=True, text=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["SUDO"])


if __name__ == "__main__":
    unittest.main()
