from __future__ import annotations

import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gp_control_plane.zapret2 import (
    BLOCKCHECK_ENV_KEYS,
    ROOT_HELPER_RECORD_RETRY_SECONDS,
    ROOT_HELPER_RECORD_WAIT_SECONDS,
    ROOT_HELPER_SUPERVISOR_READY_WAIT_SECONDS,
    ROOT_HELPER_ATTESTATION_PENDING_MESSAGE,
    MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS,
    _blockcheck_nft_tables,
    _cleanup_nft_blockcheck_tables,
    acknowledge_registered_process_run_terminal,
    _signal_process_group,
    cleanup_nft_blockcheck_tables,
    signal_registered_process_run,
    _stop_process_group,
    check_install,
    check_install_cached,
    clear_install_check_cache,
    root_command,
    root_helper_status,
    recover_quarantined_process_run,
    recover_registered_process_runs,
)
from gp_control_plane.core_api import preflight_payload


_ROOT_HELPER_TRUSTED_PATH_SETUP = "PATH='/usr/sbin:/usr/bin:/sbin:/bin'\nexport PATH\nreadonly PATH\n"


def _root_helper_test_source(helper: Path) -> str:
    """Return a fixture copy whose explicit command shims remain reachable."""
    return helper.read_text(encoding="utf-8").replace(_ROOT_HELPER_TRUSTED_PATH_SETUP, "", 1).replace(
        '${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}',
        '${GP_ROOT_HELPER_CONFIG:-/nonexistent/gp-test-root-helper-config}',
        1,
    )


class Zapret2Tests(unittest.TestCase):
    def _bounded_owned_cleanup(
        self,
        managed: subprocess.Popen[str],
        helper: Path,
        run_id: str,
        env: dict[str, str],
        registry: Path,
        root: Path,
        extra_pids: tuple[int, ...] = (),
    ) -> None:
        """Leave no fixture process if a bounded owned-run test times out.

        signal-run is attempted first, but before the root record exists it
        cannot attest any process.  In that narrow case TERMing the outer
        helper is safe: its own trap owns the unattested supervisor and reaps
        it before the TemporaryDirectory can disappear.
        """
        if managed.poll() is None:
            try:
                subprocess.run(["sh", str(helper), "signal-run", run_id, "TERM"], env=env, check=False, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.TimeoutExpired, OSError):
                pass
        if managed.poll() is None and not (registry / run_id).exists():
            managed.terminate()
        if managed.poll() is None:
            try:
                managed.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(["sh", str(helper), "signal-run", run_id, "KILL"], env=env, check=False, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                if managed.poll() is None:
                    managed.terminate()
                try:
                    managed.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    managed.kill()
                    managed.wait(timeout=3)
        self.assertIsNotNone(managed.returncode)
        # Close both pipes after every timeout/error route as well as normal
        # completion; wait() alone leaves Popen's text wrappers open.
        managed.communicate(timeout=1)
        self.assertFalse(Path(f"/proc/{managed.pid}").exists())
        trap_reap_failure: AssertionError | None = None
        for pid in extra_pids:
            # This assertion intentionally happens before any voluntary
            # TERM/KILL.  The pre-record test proves that the outer helper's
            # trap reaped its own supervisor; fixture cleanup must not turn a
            # failed trap into a passing result.
            try:
                _wait_for_proc_path_to_disappear(pid, timeout=3)
            except AssertionError as error:
                trap_reap_failure = error
                # Safe teardown still targets only the test's recorded,
                # unattested PID.  It is cleanup after a failed assertion,
                # never evidence of successful root-helper reaping.
                if Path(f"/proc/{pid}").exists():
                    os.kill(pid, signal.SIGTERM)
                    try:
                        _wait_for_proc_path_to_disappear(pid, timeout=3)
                    except AssertionError:
                        os.kill(pid, signal.SIGKILL)
                        _wait_for_proc_path_to_disappear(pid, timeout=3)
        self.assertFalse((registry / run_id).exists())
        lock_dir = registry / f".{run_id}.lock"
        self.assertFalse(
            lock_dir.exists(),
            f"pre-record lock remained after outer-helper cleanup: "
            f"{[(path.name, path.is_dir(), path.is_symlink()) for path in lock_dir.iterdir()] if lock_dir.exists() else []}",
        )
        self.assertFalse((registry / f".{run_id}.terminal").exists())
        self.assertEqual([path for path in root.glob("gp-root-helper.*") if path.is_dir()], [])
        if trap_reap_failure is not None:
            self.fail(f"outer helper exited without reaping its pre-record supervisor: {trap_reap_failure}")

    def test_root_helper_accepts_only_blockcheck_digits_table_names(self) -> None:
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "nft.log"
            (fake_bin / "nft").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GP_TEST_NFT_LOG"\n', encoding="utf-8")
            (fake_bin / "nft").chmod(0o700)
            helper_copy = root / "helper.sh"
            helper_copy.write_text(
                _root_helper_test_source(helper).replace(
                    "\nrequire_root\n\ncommand=", "\n: # parser test intentionally bypasses root requirement\n\ncommand=", 1
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "PATH": _posix_shell_path(fake_bin), "GP_TEST_NFT_LOG": _posix_shell_path(calls)}
            accepted = subprocess.run([shell, _posix_shell_path(helper_copy), "nft-delete-blockcheck-table", "inet", "blockcheck1"], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            rejected = subprocess.run([shell, _posix_shell_path(helper_copy), "nft-delete-blockcheck-table", "inet", "blockcheck1-other"], env=env, text=True, capture_output=True, check=False)
            self.assertEqual(rejected.returncode, 126)
            self.assertIn("unsupported nft table", rejected.stderr)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["delete table inet blockcheck1"])

    def test_public_cleanup_nft_blockcheck_tables_delegates_to_private_cleanup(self) -> None:
        with mock.patch("gp_control_plane.zapret2._cleanup_nft_blockcheck_tables") as private_cleanup:
            cleanup_nft_blockcheck_tables()

        private_cleanup.assert_called_once_with()

    def test_cleanup_nft_blockcheck_tables_uses_root_helper_after_direct_delete_is_denied(self) -> None:
        table = "blockcheck2427371"
        with (
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/sbin/nft"),
            mock.patch(
                "gp_control_plane.zapret2.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["nft", "list", "tables"], 0, f"table inet {table}\n", ""),
                    subprocess.CompletedProcess(["nft", "delete", "table", "inet", table], 1, "", "Operation not permitted"),
                ],
            ) as direct_nft,
            mock.patch("gp_control_plane.zapret2._run_root_helper") as root_helper,
        ):
            _cleanup_nft_blockcheck_tables()

        direct_nft.assert_has_calls(
            [
                mock.call(["/usr/sbin/nft", "list", "tables"], text=True, capture_output=True, check=False),
                mock.call(
                    ["/usr/sbin/nft", "delete", "table", "inet", table], text=True, capture_output=True, check=False
                ),
            ]
        )
        root_helper.assert_called_once_with(["nft-delete-blockcheck-table", "inet", table])

    def test_root_helper_env_whitelist_matches_backend_keys(self) -> None:
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        text = helper.read_text(encoding="utf-8")
        match = re.search(r"case \"\$key\" in\s+([^)]+)\)", text)
        self.assertIsNotNone(match)
        helper_keys = set(match.group(1).strip().split("|"))

        self.assertEqual(helper_keys, set(BLOCKCHECK_ENV_KEYS))

    def test_root_owned_target_exit_126_is_returned_not_reclassified_as_invalid_status(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "blockcheck2.sh"
            target.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
            target.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            run_id = "target-exit-126"
            env = {**os.environ, "GP_ROOT_HELPER_CONFIG": str(config), "GP_ROOT_HELPER_RUN_DIR": str(registry)}
            managed = subprocess.Popen(
                ["sh", str(helper), "run-owned", "target-exit-126", str(target)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                try:
                    _, stderr = managed.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    self.fail("run-owned valid-126 did not terminate within 8 seconds")
                self.assertEqual(managed.returncode, 126, stderr)
                self.assertNotIn("managed target status is invalid", stderr)
            finally:
                self._bounded_owned_cleanup(managed, helper, run_id, env, registry, root)

    def test_root_owned_multidomain_target_exit_126_is_bounded_and_cleans_runner(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires root Linux with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "blockcheck2.sh"
            # write_multidomain_runner retains only the pre-fsleep_setup prologue.
            # Supply its harmless bootstrap functions and make its terminal
            # cleanup preserve the target's exact nonzero result.
            source.write_text(
                "#!/bin/sh\nUNAME=CYGWIN\nSKIP_PKTWS=1\nIPVS=\n"
                "fsleep_setup() { :; }\nfix_sbin_path() { :; }\n"
                "check_system() { :; }\ncheck_already() { :; }\n"
                "check_prerequisites() { :; }\ncheck_dns() { :; }\n"
                "check_virt() { :; }\nask_params() { :; }\n"
                "cleanup() { return 126; }\nfsleep_setup\n",
                encoding="utf-8",
            )
            source.chmod(0o700)
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            run_id = "multi-target-126"
            registry = root / "runs"
            env = {**os.environ, "GP_ROOT_HELPER_CONFIG": str(config), "GP_ROOT_HELPER_RUN_DIR": str(registry), "TMPDIR": str(root)}
            managed = subprocess.Popen(
                ["sh", str(helper), "run-multidomain-owned", run_id, str(source)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                try:
                    _, stderr = managed.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    self.fail("run-multidomain-owned valid-126 did not terminate within 8 seconds")
                self.assertEqual(managed.returncode, 126, stderr)
                self.assertNotIn("managed target status is invalid", stderr)
            finally:
                self._bounded_owned_cleanup(managed, helper, run_id, env, registry, root)

    def test_root_owned_pre_record_ready_hang_terminates_outer_helper_and_supervisor(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires root Linux with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            gates = root / "gates"
            gates.mkdir(mode=0o700)
            helper_copy = root / "helper.sh"
            helper_copy.write_text(
                _root_helper_test_source(helper).replace(
                    "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'", f"DISCOVERY_GATE_DIR='{_posix_shell_path(gates)}'", 1
                ),
                encoding="utf-8",
            )
            helper_copy.chmod(0o700)
            supervisor_pid = root / "supervisor.pid"
            fake_setsid = fake_bin / "setsid"
            fake_setsid.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$$\" > \"$GP_TEST_SUPERVISOR_PID\"\n"
                # Deliberately do not start the real supervisor command: this
                # is an absent-ready, pre-record fixture.  exec avoids a
                # shell-child/zombie ambiguity, so termination of the outer
                # Popen must make the helper's existing trap TERM and reap
                # this recorded fixture supervisor.
                "exec /bin/sleep 2147483647\n",
                encoding="utf-8",
            )
            fake_setsid.chmod(0o700)
            target = root / "blockcheck2.sh"
            target.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
            target.chmod(0o700)
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            run_id = "pre-record-ready-hang"
            registry = root / "runs"
            env = {
                **os.environ,
                "PATH": f"{_posix_shell_path(fake_bin)}:{os.environ.get('PATH', '')}",
                "GP_TEST_SUPERVISOR_PID": str(supervisor_pid),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
            }
            managed = subprocess.Popen(
                ["sh", str(helper_copy), "run-owned", run_id, str(target)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            observed_supervisor_pid: int | None = None
            cleanup_evidence = os.environ.get("GP_TEST_PRE_RECORD_CLEANUP_EVIDENCE")
            try:
                _wait_for_path(supervisor_pid)
                observed_supervisor_pid = int(supervisor_pid.read_text(encoding="utf-8").strip())
                with self.assertRaises(subprocess.TimeoutExpired):
                    managed.communicate(timeout=1)
                self.assertFalse((registry / run_id).exists())
            finally:
                self._bounded_owned_cleanup(
                    managed,
                    helper_copy,
                    run_id,
                    env,
                    registry,
                    root,
                    (() if observed_supervisor_pid is None else (observed_supervisor_pid,)),
                )
                if cleanup_evidence is not None and observed_supervisor_pid is not None:
                    evidence_path = Path(cleanup_evidence)
                    evidence_path.write_text(
                        "pre-record-cleanup-v1\n"
                        f"outer-pid={managed.pid} proc=absent\n"
                        f"supervisor-pid={observed_supervisor_pid} proc=absent\n"
                        f"record={registry / run_id} absent\n"
                        f"lock={registry / f'.{run_id}.lock'} absent\n"
                        f"terminal={registry / f'.{run_id}.terminal'} absent\n"
                        "runner-dirs=0\n"
                        "supervisor-fallback=not-needed\n",
                        encoding="utf-8",
                    )

    def test_target_status_metadata_contract_is_fail_closed(self) -> None:
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        text = helper.read_text(encoding="utf-8")
        reader = text.split("read_owned_run_status() {", 1)[1].split("wait_for_owned_run_status()", 1)[0]
        self.assertIn("[ ! -L \"$status_file\" ] || return 2", reader)
        self.assertLess(reader.index("[ ! -L \"$status_file\" ] || return 2"), reader.index("[ -e \"$status_file\" ] || return 1"))
        self.assertIn("[ -f \"$status_file\" ] || return 2", reader)
        self.assertIn("stat -c '%u:%g:%a' \"$status_file\"", reader)
        self.assertIn("= '0:0:600'", reader)
        owned = text.split("run_owned_process() {", 1)[1].split("run_owned_target() {", 1)[0]
        self.assertIn("quarantine_invalid_owned_lifecycle", owned)
        self.assertIn('fail "managed target status is invalid"', owned)

    def test_root_helper_pins_trusted_path_for_generic_command_lookup(self) -> None:
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        if subprocess.run([shell, "-c", "[ -x /usr/bin/readlink ]"], check=False).returncode != 0:
            self.skipTest("requires readlink at the helper's trusted /usr/bin location")

        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "hostile-bin"
            fake_bin.mkdir()
            hostile_readlink_called = root / "hostile-readlink-called"
            (fake_bin / "readlink").write_text(
                "#!/bin/sh\n"
                "printf invoked > \"$GP_TEST_HOSTILE_READLINK_CALLED\"\n"
                "exit 99\n",
                encoding="utf-8",
            )
            (fake_bin / "readlink").chmod(0o700)
            config = root / "gp-root-helper.conf"
            config.write_text(f"PATH={shlex.quote(_posix_shell_path(fake_bin))}\n", encoding="utf-8")
            library = root / "root-helper-library.sh"
            library.write_text(helper.read_text(encoding="utf-8").split("\nrequire_root\n", 1)[0] + "\n", encoding="utf-8")

            command = [
                shell,
                "-c",
                '. "$1"; real_path "$2"',
                "root-helper-trusted-path",
                _posix_shell_path(library),
                "/",
            ]
            hostile_env = {
                **os.environ,
                "PATH": _posix_shell_path(fake_bin),
                "GP_ROOT_HELPER_CONFIG": _posix_shell_path(root / "missing-config"),
                "GP_TEST_HOSTILE_READLINK_CALLED": _posix_shell_path(hostile_readlink_called),
            }
            inherited_path = subprocess.run(command, env=hostile_env, text=True, capture_output=True, check=False)

            self.assertEqual(inherited_path.returncode, 0, inherited_path.stderr)
            self.assertEqual(inherited_path.stdout, "/\n")
            self.assertFalse(hostile_readlink_called.exists())

            config_env = {**hostile_env, "GP_ROOT_HELPER_CONFIG": _posix_shell_path(config)}
            config_override = subprocess.run(command, env=config_env, text=True, capture_output=True, check=False)

            self.assertNotEqual(config_override.returncode, 0)
            self.assertFalse(hostile_readlink_called.exists())

    def test_check_install_reports_available_paths(self) -> None:
        def fake_which(name: str) -> str | None:
            return {"nfqws2": "/usr/bin/nfqws2", "blockcheck2.sh": "/usr/bin/blockcheck2.sh"}.get(name)

        with (
            tempfile.TemporaryDirectory() as raw,
            mock.patch.dict(os.environ, {"GP_ROOT_HELPER": str(Path(raw) / "missing-helper")}),
            mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=fake_which),
        ):
            result = check_install()

        self.assertTrue(result["nfqws2_found"])
        self.assertEqual(result["nfqws2_path"], "/usr/bin/nfqws2")
        self.assertTrue(result["blockcheck_found"])
        self.assertEqual(result["blockcheck_path"], "/usr/bin/blockcheck2.sh")
        self.assertFalse(result["root_helper_ready"])
        self.assertFalse(result["ready"])
        self.assertTrue(any(item["id"] == "root-helper" and not item["ok"] for item in result["diagnostics"]))

    def test_check_install_reports_russian_message_for_missing_nfqws2(self) -> None:
        with mock.patch("gp_control_plane.zapret2.shutil.which", return_value=None):
            result = check_install()

        diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
        self.assertEqual(
            diagnostics["nfqws2"]["message"],
            "не найден в PATH; установите zapret2 или проверьте ссылку на nfqws2",
        )

    def test_unavailable_root_helper_has_russian_message_and_raw_diagnostic_reason(self) -> None:
        cases = (
            ("root-helper not found at /helper/gp-root-helper", {"sudo": "/usr/bin/sudo"}, False),
            ("sudo command not found", {}, True),
        )
        for raw_reason, commands, helper_exists in cases:
            with (
                self.subTest(raw_reason=raw_reason),
                mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
                mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
                mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=helper_exists),
                mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
                mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=lambda name: commands.get(name)),
            ):
                result = check_install()

            diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
            root_helper = diagnostics["root-helper"]
            self.assertEqual(root_helper["message"], "служба с повышенными правами недоступна; запустите Linux-установщик")
            self.assertEqual(root_helper["details"], {"reason": raw_reason})
            self.assertEqual(result["root_helper_error"], raw_reason)

    def test_preflight_preserves_root_helper_diagnostic_details(self) -> None:
        zapret = {
            "diagnostics": [
                {
                    "id": "root-helper",
                    "ok": False,
                    "message": "служба с повышенными правами недоступна; запустите Linux-установщик",
                    "details": {"reason": "sudo command not found"},
                }
            ]
        }
        with mock.patch("gp_control_plane.core_api.check_install_cached", return_value=zapret):
            preflight = preflight_payload(mock.sentinel.config)

        self.assertEqual(preflight["checks"][0]["message"], "служба с повышенными правами недоступна; запустите Linux-установщик")
        self.assertEqual(preflight["checks"][0]["details"], {"reason": "sudo command not found"})

    def test_check_install_reports_human_diagnostics(self) -> None:
        def fake_which(name: str) -> str | None:
            return {
                "nfqws2": "/usr/bin/nfqws2",
                "blockcheck2.sh": "/usr/bin/blockcheck2.sh",
                "curl": "/usr/bin/curl",
                "nft": "/usr/sbin/nft",
                "sudo": "/usr/bin/sudo",
            }.get(name)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", side_effect=fake_which),
            mock.patch("gp_control_plane.zapret2.subprocess.run", return_value=subprocess.CompletedProcess(["check"], 0, "", "")),
        ):
            result = check_install()

        self.assertTrue(result["ready"])
        diagnostics = {str(item["id"]): item for item in result["diagnostics"]}
        self.assertTrue(diagnostics["nfqws2"]["ok"])
        self.assertTrue(diagnostics["blockcheck"]["ok"])
        self.assertTrue(diagnostics["root-helper"]["ok"])
        self.assertTrue(diagnostics["curl"]["ok"])
        self.assertTrue(diagnostics["nft"]["ok"])
        self.assertIn("/usr/bin/nfqws2", str(diagnostics["nfqws2"]["message"]))

    def test_root_helper_status_uses_sudo_non_interactively(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
        ):
            status = root_helper_status()

        self.assertTrue(status["ready"])
        self.assertEqual(calls[0], ["/usr/bin/sudo", "-n", "/helper/gp-root-helper", "check"])

    def test_check_install_cached_reuses_root_helper_result(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        clear_install_check_cache()
        try:
            with (
                mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
                mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
                mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
                mock.patch("gp_control_plane.zapret2.os.access", return_value=True),
                mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
                mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
            ):
                first = check_install_cached(ttl_seconds=60)
                second = check_install_cached(ttl_seconds=60)
        finally:
            clear_install_check_cache()

        self.assertTrue(first["root_helper_ready"])
        self.assertEqual(second["root_helper_path"], "/helper/gp-root-helper")
        self.assertEqual(len(calls), 1)

    def test_root_command_wraps_blockcheck_with_helper_and_env(self) -> None:
        env = {"BATCH": "1", "DOMAINS": "youtube.com", "ENABLE_HTTP3": "1", "IGNORED": "x"}

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.require_root_helper_ready"),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
        ):
            command = root_command(
                ["/opt/zapret2/blockcheck2.sh"], env=env, pass_env_keys=BLOCKCHECK_ENV_KEYS, run_id="run-owned"
            )

        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/helper/gp-root-helper",
                "run-owned-env",
                "run-owned",
                "BATCH=1",
                "DOMAINS=youtube.com",
                "ENABLE_HTTP3=1",
                "--",
                "/opt/zapret2/blockcheck2.sh",
            ],
        )

    def test_root_command_wraps_multidomain_with_helper_owned_runner(self) -> None:
        env = {"BATCH": "1", "DOMAINS": "youtube.com", "GP_MD_CURL_PARALLELISM": "30"}

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.require_root_helper_ready"),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
        ):
            command = root_command(
                ["/opt/zapret2/blockcheck2.sh"],
                env=env,
                pass_env_keys=BLOCKCHECK_ENV_KEYS,
                helper_command="run-multidomain",
                run_id="run-owned",
            )

        self.assertEqual(
            command,
            [
                "/usr/bin/sudo",
                "-n",
                "/helper/gp-root-helper",
                "run-multidomain-owned-env",
                "run-owned",
                "BATCH=1",
                "DOMAINS=youtube.com",
                "GP_MD_CURL_PARALLELISM=30",
                "--",
                "/opt/zapret2/blockcheck2.sh",
            ],
        )

    def test_root_caller_still_uses_the_discovery_helper_gate(self) -> None:
        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch("gp_control_plane.zapret2.require_root_helper_ready"),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
        ):
            command = root_command(["/opt/zapret2/blockcheck2.sh"], helper_command="run-multidomain")

        self.assertEqual(command, ["/helper/gp-root-helper", "run-multidomain", "/opt/zapret2/blockcheck2.sh"])

    def test_stop_process_group_terminates_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=hasattr(os, "setsid"))
        try:
            _stop_process_group(process)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()

    def test_managed_stop_delegates_to_root_without_signalling_local_group(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.acknowledge_registered_process_run_terminal") as acknowledge_terminal,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            _stop_process_group(process, run_id="managed-run")

        self.assertEqual(
            signal_registered.call_args_list,
            [mock.call("managed-run", "TERM")],
        )
        acknowledge_terminal.assert_called_once_with("managed-run")
        killpg.assert_not_called()
        process.wait.assert_called_once_with(timeout=MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)

    def test_managed_stop_reaps_launcher_before_acknowledging_verified_terminal(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        events: list[str] = []

        def signal_run(_run_id: str, _signal_name: str) -> None:
            events.append("signal")

        def wait_for_launcher(*, timeout: float) -> int:
            self.assertEqual(timeout, MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)
            events.append("reaped")
            return 143

        def acknowledge_terminal(_run_id: str) -> None:
            events.append("acknowledged")

        process.wait.side_effect = wait_for_launcher
        with (
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run", side_effect=signal_run),
            mock.patch(
                "gp_control_plane.zapret2.acknowledge_registered_process_run_terminal",
                side_effect=acknowledge_terminal,
            ),
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            _stop_process_group(process, run_id="managed-run")

        self.assertEqual(events, ["signal", "reaped", "acknowledged"])
        killpg.assert_not_called()

    def test_managed_stop_quarantines_terminal_ack_integrity_failure_without_local_signal(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 143

        with (
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch(
                "gp_control_plane.zapret2.acknowledge_registered_process_run_terminal",
                side_effect=RuntimeError("gp-root-helper: run terminal is unsafe: managed-run"),
            ) as acknowledge_terminal,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            with self.assertRaisesRegex(RuntimeError, "run terminal is unsafe"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        acknowledge_terminal.assert_called_once_with("managed-run")
        killpg.assert_not_called()

    def test_managed_stop_propagates_stale_root_record_without_signalling_local_group(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = None

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch(
                "gp_control_plane.zapret2.signal_registered_process_run",
                side_effect=RuntimeError("gp-root-helper: registered process is stale or invalid"),
            ) as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
            mock.patch("gp_control_plane.zapret2.signal.SIGTERM", "term-signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stale or invalid"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()
        process.wait.assert_not_called()

    def test_managed_stop_propagates_integrity_failure_even_if_message_mentions_stale_record(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch(
                "gp_control_plane.zapret2.signal_registered_process_run",
                side_effect=RuntimeError("gp-root-helper: integrity failure; registered process is stale or invalid"),
            ) as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
            mock.patch("gp_control_plane.zapret2.signal.SIGTERM", "term-signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "integrity failure"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()
        process.wait.assert_not_called()

    def test_managed_signal_does_not_signal_local_group_when_helper_fails(self) -> None:
        process = mock.Mock(pid=12345)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch(
                "gp_control_plane.zapret2.signal_registered_process_run",
                side_effect=RuntimeError("root-helper rejected registered process signal"),
            ) as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
            mock.patch("gp_control_plane.zapret2.signal.SIGTERM", "term-signal"),
        ):
            with self.assertRaisesRegex(RuntimeError, "root-helper rejected"):
                _signal_process_group("TERM", process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()

    def test_managed_stop_refuses_local_kill_after_registered_signal_timeout(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired(["blockcheck2.sh"], 5)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            with self.assertRaisesRegex(RuntimeError, "managed root process did not terminate"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()
        process.wait.assert_called_once_with(timeout=MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)

    def test_root_managed_stop_delegates_to_helper_without_signalling_local_group(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = None

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.acknowledge_registered_process_run_terminal") as acknowledge_terminal,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        acknowledge_terminal.assert_called_once_with("managed-run")
        killpg.assert_not_called()
        process.wait.assert_called_once_with(timeout=MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)

    def test_managed_stop_accepts_launcher_exit_after_registered_signal_record_reconciliation(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 143

        with (
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.acknowledge_registered_process_run_terminal") as acknowledge_terminal,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        acknowledge_terminal.assert_called_once_with("managed-run")
        process.wait.assert_called_once_with(timeout=MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)
        killpg.assert_not_called()

    def test_root_managed_stop_propagates_helper_failure_without_local_signal(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch(
                "gp_control_plane.zapret2.signal_registered_process_run",
                side_effect=RuntimeError("root-helper rejected registered process signal"),
            ) as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            with self.assertRaisesRegex(RuntimeError, "root-helper rejected"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()
        process.wait.assert_not_called()

    def test_root_managed_stop_refuses_local_kill_after_registered_signal_timeout(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired(["blockcheck2.sh"], 5)

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch("gp_control_plane.zapret2.signal_registered_process_run") as signal_registered,
            mock.patch("gp_control_plane.zapret2.os.killpg", create=True) as killpg,
        ):
            with self.assertRaisesRegex(RuntimeError, "managed root process did not terminate"):
                _stop_process_group(process, run_id="managed-run")

        signal_registered.assert_called_once_with("managed-run", "TERM")
        killpg.assert_not_called()
        process.wait.assert_called_once_with(timeout=MANAGED_ROOT_LAUNCHER_EXIT_WAIT_SECONDS)

    def test_stop_process_group_propagates_timeout_after_kill_wait(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        timeout = subprocess.TimeoutExpired(["blockcheck2.sh"], 5)
        process.wait.side_effect = [timeout, timeout]

        with (
            mock.patch("gp_control_plane.zapret2._signal_process_group") as signal_process_group,
            mock.patch("gp_control_plane.zapret2._signal_local_process_group") as signal_local_process_group,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                _stop_process_group(process)

        signal_process_group.assert_called_once_with("TERM", process, None)
        signal_local_process_group.assert_called_once_with("KILL", process)
        self.assertEqual(process.wait.call_count, 2)

    def test_blockcheck_nft_tables_extracts_only_temporary_tables(self) -> None:
        output = """
table inet blockcheck1460063
table ip filter
table inet blockcheckabc
table inet blockcheck42
"""

        self.assertEqual(_blockcheck_nft_tables(output), [("inet", "blockcheck1460063"), ("inet", "blockcheck42")])

    def test_registered_process_signal_uses_only_the_run_record(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._root_helper_path", return_value="/helper/gp-root-helper"),
            mock.patch("gp_control_plane.zapret2.Path.is_file", return_value=True),
            mock.patch("gp_control_plane.zapret2.shutil.which", return_value="/usr/bin/sudo"),
            mock.patch("gp_control_plane.zapret2.subprocess.run", side_effect=fake_run),
        ):
            signal_registered_process_run("a" * 32, "TERM")

        self.assertEqual(calls, [["/usr/bin/sudo", "-n", "/helper/gp-root-helper", "signal-run", "a" * 32, "TERM"]])

    def test_root_registered_process_signal_uses_root_helper(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch("gp_control_plane.zapret2._run_recovery_root_helper", return_value=completed) as root_helper,
            mock.patch("gp_control_plane.zapret2._run_root_helper") as sudo_helper,
        ):
            signal_registered_process_run("a" * 32, "TERM")

        root_helper.assert_called_once_with(["signal-run", "a" * 32, "TERM"])
        sudo_helper.assert_not_called()

    def test_terminal_acknowledgement_uses_root_helper_and_preserves_fail_closed_error(self) -> None:
        run_id = "a" * 32
        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
            mock.patch(
                "gp_control_plane.zapret2._run_recovery_root_helper",
                return_value=subprocess.CompletedProcess([], 126, "", "gp-root-helper: run terminal is unsafe\n"),
            ) as root_helper,
            mock.patch("gp_control_plane.zapret2._run_root_helper") as sudo_helper,
        ):
            with self.assertRaisesRegex(RuntimeError, "run terminal is unsafe"):
                acknowledge_registered_process_run_terminal(run_id)

        root_helper.assert_called_once_with(["ack-run-terminal", run_id])
        sudo_helper.assert_not_called()

    def test_immediate_stop_waits_for_root_attestation_before_signalling(self) -> None:
        for phase in ("v1", "v2-before-go"):
            with self.subTest(phase=phase):
                run_id = f"immediate-stop-{phase}"
                calls: list[list[str]] = []
                responses = [
                    subprocess.CompletedProcess([], 126, "", f"gp-root-helper: {ROOT_HELPER_ATTESTATION_PENDING_MESSAGE}: {run_id}\n"),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]

                def fake_helper(command: list[str]) -> subprocess.CompletedProcess[str]:
                    calls.append(command)
                    return responses.pop(0)

                with (
                    mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
                    mock.patch("gp_control_plane.zapret2._run_root_helper", side_effect=fake_helper),
                    mock.patch("gp_control_plane.zapret2.time.sleep") as sleep,
                ):
                    signal_registered_process_run(run_id, "TERM")

                self.assertEqual(calls, [["signal-run", run_id, "TERM"], ["signal-run", run_id, "TERM"]])
                sleep.assert_called_once()

    def test_root_helper_marks_only_valid_v1_and_v2_before_go_as_pending(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")
        signal_run = helper.split("signal_registered_process_run() {", 1)[1].split("ensure_recovery_run_registry() {", 1)[0]
        lifecycle_validation = helper.split("recovery_validate_run_lifecycle_dir() {", 1)[1].split(
            "recovery_validate_run_lock() {", 1
        )[0]

        self.assertIn('read_owned_run_ready "$ready_file" >/dev/null || read_owned_run_attestation "$ready_file" >/dev/null', signal_run)
        self.assertIn('fail "root run attestation is pending: $run_id"', signal_run)
        self.assertIn('read_owned_run_ready "$ready_file" >/dev/null || read_owned_run_attestation "$ready_file" >/dev/null', lifecycle_validation)
        self.assertIn('[ "$gate_present" = 1 ] || return 1', lifecycle_validation)
        self.assertIn('[ "$status_present" = 0 ] && [ "$signal_present" = 0 ] || return 1', lifecycle_validation)

    def test_root_helper_keeps_signal_terminal_and_invalid_status_quarantine_shapes_separate(self) -> None:
        """A signal receipt belongs to a terminal transition, never a recordless quarantine lock."""
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")
        signal_locked = helper.split("signal_registered_process_run_locked() {", 1)[1].split(
            "signal_registered_terminal_is_complete() {", 1
        )[0]
        terminal_recovery = helper.split("recover_terminal_run() {", 1)[1].split(
            "remove_recovery_run_artifacts() {", 1
        )[0]
        invalid_recovery = helper.split("recover_invalid_status_quarantine_locked() {", 1)[1].split(
            "recover_invalid_status_quarantine() {", 1
        )[0]
        run_owned = helper.split("run_owned_process() {", 1)[1].split("run_owned_target() {", 1)[0]
        supervisor_body = run_owned.split("setsid /bin/sh -c '", 1)[1].split("' gp-owned-supervisor", 1)[0]

        self.assertLess(
            signal_locked.index('write_owned_run_signal_delivery "$signal_file" "$signal" "$pid" "$pgid" "$marker"'),
            signal_locked.index('rm -f -- "$record"'),
        )
        self.assertIn('signal_registered_terminal_is_complete "$run_id"', terminal_recovery)
        self.assertIn('recovery_validate_record "$run_id" || return 2', invalid_recovery)
        self.assertNotIn('read_owned_run_signal_delivery', invalid_recovery)
        self.assertNotIn('signal-delivery', invalid_recovery)
        self.assertIn('revalidate_signal_run_lifecycle_after_gate "$run_id" || return 2', signal_locked)
        self.assertLess(
            signal_locked.index('revalidate_signal_run_lifecycle_after_gate "$run_id" || return 2'),
            signal_locked.index('terminate_known_process_group "$pid" "$pgid" "$marker" "$signal"'),
        )
        self.assertIn('exec 8<>"$lifecycle_gate" || exit 125', supervisor_body)
        self.assertIn('flock -x 8 || exit 125', supervisor_body)
        self.assertIn('[ "$(cat "$record")" = "helper-v1 $$ $$ $ready_marker" ] || exit 125', supervisor_body)
        self.assertIn('[ ! -e "$lock_dir/signal-delivery" ] && [ ! -L "$lock_dir/signal-delivery" ] || exit 125', supervisor_body)
        self.assertLess(supervisor_body.index('flock -x 8 || exit 125'), supervisor_body.index('mv -f "$tmp_status" "$status_file"'))
        self.assertLess(supervisor_body.index('mv -f "$tmp_status" "$status_file"'), supervisor_body.index('flock -u 8 || exit 125'))
        self.assertLess(supervisor_body.index('flock -u 8 || exit 125'), supervisor_body.index('trap "" HUP INT TERM'))

    def test_immediate_stop_waits_through_the_root_helper_supervisor_handshake(self) -> None:
        run_id = "late-root-record"
        clock = {"now": 0.0}
        calls: list[list[str]] = []

        def fake_helper(command: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if clock["now"] < ROOT_HELPER_SUPERVISOR_READY_WAIT_SECONDS:
                return subprocess.CompletedProcess([], 126, "", f"gp-root-helper: {ROOT_HELPER_ATTESTATION_PENDING_MESSAGE}\n")
            return subprocess.CompletedProcess([], 0, "", "")

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._run_root_helper", side_effect=fake_helper),
            mock.patch("gp_control_plane.zapret2.time.monotonic", side_effect=lambda: clock["now"]),
            mock.patch("gp_control_plane.zapret2.time.sleep", side_effect=fake_sleep),
        ):
            signal_registered_process_run(run_id, "TERM")

        self.assertEqual(ROOT_HELPER_SUPERVISOR_READY_WAIT_SECONDS, 10.0)
        self.assertEqual(ROOT_HELPER_RECORD_WAIT_SECONDS, 12.0)
        self.assertEqual(ROOT_HELPER_RECORD_RETRY_SECONDS, 0.25)
        self.assertEqual(ROOT_HELPER_ATTESTATION_PENDING_MESSAGE, "root run attestation is pending")
        self.assertEqual(clock["now"], ROOT_HELPER_SUPERVISOR_READY_WAIT_SECONDS)
        self.assertEqual(calls[-1], ["signal-run", run_id, "TERM"])
        self.assertGreater(len(calls), 2)

    def test_signal_registered_process_stops_retrying_at_the_record_wait_deadline(self) -> None:
        failure = subprocess.CompletedProcess([], 126, "", f"gp-root-helper: {ROOT_HELPER_ATTESTATION_PENDING_MESSAGE}\n")
        with (
            mock.patch("gp_control_plane.zapret2._is_root", return_value=False),
            mock.patch("gp_control_plane.zapret2._run_root_helper", return_value=failure) as helper,
            mock.patch("gp_control_plane.zapret2.ROOT_HELPER_RECORD_WAIT_SECONDS", 0.1),
            mock.patch("gp_control_plane.zapret2.time.monotonic", side_effect=[10.0, 10.1]),
            mock.patch("gp_control_plane.zapret2.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "attestation is pending"):
                signal_registered_process_run("timeout-race", "TERM")

        self.assertEqual(helper.call_count, 1)
        sleep.assert_not_called()

    def test_root_recovery_failure_blocks_only_a_quarantined_runtime(self) -> None:
        failed = subprocess.CompletedProcess([], 126, "", "gp-root-helper: root record mismatch\n")
        with (
            mock.patch("gp_control_plane.zapret2._run_recovery_root_helper", return_value=failed) as helper,
        ):
            self.assertFalse(recover_registered_process_runs())
            with self.assertRaisesRegex(RuntimeError, "root record mismatch"):
                recover_quarantined_process_run("quarantined-run")

        self.assertEqual(
            helper.call_args_list,
            [mock.call(["recover-runs"]), mock.call(["recover-run", "quarantined-run"])],
        )

    def test_quarantine_recovery_requires_a_matching_root_receipt_and_runs_as_root(self) -> None:
        run_id = "quarantined-run"
        with tempfile.TemporaryDirectory() as raw:
            helper_path = Path(raw) / "gp-root-helper"
            helper_path.write_text("#!/bin/sh\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [str(helper_path), "recover-run", run_id],
                0,
                f"recovered-run-v1 {run_id}\n",
                "",
            )
            with (
                mock.patch("gp_control_plane.zapret2._is_root", return_value=True),
                mock.patch("gp_control_plane.zapret2._root_helper_path", return_value=str(helper_path)),
                mock.patch("gp_control_plane.zapret2.subprocess.run", return_value=completed) as run,
            ):
                recover_quarantined_process_run(run_id)

        run.assert_called_once_with([str(helper_path), "recover-run", run_id], text=True, capture_output=True, check=False)

    def test_quarantine_recovery_rejects_a_receipt_for_another_run(self) -> None:
        with mock.patch(
            "gp_control_plane.zapret2._run_recovery_root_helper",
            return_value=subprocess.CompletedProcess([], 0, "recovered-run-v1 another-run\n", ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "recovery proof is invalid"):
                recover_quarantined_process_run("expected-run")

    def test_root_helper_exposes_no_direct_pid_registration_or_unscoped_cleanup(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        self.assertIn('  run-owned)', helper)
        self.assertIn('  run-owned-env)', helper)
        self.assertIn('  signal-run)', helper)
        self.assertIn('    with_discovery_gate signal_registered_process_run "$@"', helper)
        self.assertNotIn('  register-run)', helper)
        self.assertNotIn('  unregister-run)', helper)
        self.assertNotIn('"kill")', helper)
        self.assertNotIn('"killpg")', helper)
        self.assertNotIn("pgrep", helper)











    def test_discovery_gate_wraps_every_privileged_blockcheck_entrypoint(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        self.assertIn("DISCOVERY_GATE_FILE=\"$DISCOVERY_GATE_DIR/discovery-update.lock\"", helper)
        gate = helper.split("with_discovery_gate() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('flock -n -s 9', gate)
        self.assertIn('return 75', gate)
        self.assertIn('exec 9<>"$DISCOVERY_GATE_FILE"', gate)
        self.assertIn('[ -f "$DISCOVERY_GATE_FILE" ] && [ ! -L "$DISCOVERY_GATE_FILE" ]', helper)
        self.assertIn("if [ ! -e \"$DISCOVERY_GATE_FILE\" ] && [ ! -L \"$DISCOVERY_GATE_FILE\" ]; then", helper)
        self.assertNotIn('cat "$DISCOVERY_GATE_FILE"', gate)  # an empty stale gate file is valid
        for command in (
            'with_discovery_gate run_target "$@"',
            'with_discovery_gate run_owned_target "$@"',
            'with_discovery_gate run_multidomain_target "$@"',
            'with_discovery_gate run_owned_multidomain_target "$@"',
            'with_discovery_gate run_owned_target "$run_id" "$@"',
            'with_discovery_gate run_owned_multidomain_target "$run_id" "$@"',
        ):
            self.assertIn(command, helper)

    def test_empty_gate_file_supports_both_nonblocking_conflicts_without_root_or_systemd(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires POSIX flock")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            gate = root / "discovery-update.lock"
            gate.touch()  # stale/empty content must not affect advisory locking
            ready = root / "ready"
            holder = subprocess.Popen(
                [shell, "-c", 'exec 9<> "$1"; flock -s 9; : > "$2"; read release', "shared-holder", str(gate), str(ready)],
                stdin=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_path(ready)
                update = subprocess.run(
                    [shell, "-c", 'exec 9<> "$1"; flock -n -x 9 || exit 75', "update", str(gate)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(update.returncode, 75)
            finally:
                assert holder.stdin is not None
                holder.stdin.write("release\n")
                holder.stdin.close()
                self.assertEqual(holder.wait(timeout=5), 0)

            ready.unlink()
            holder = subprocess.Popen(
                [shell, "-c", 'exec 9<> "$1"; flock -x 9; : > "$2"; read release', "exclusive-holder", str(gate), str(ready)],
                stdin=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_path(ready)
                discovery = subprocess.run(
                    [shell, "-c", 'exec 9<> "$1"; flock -n -s 9 || exit 75', "discovery", str(gate)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(discovery.returncode, 75)
            finally:
                assert holder.stdin is not None
                holder.stdin.write("release\n")
                holder.stdin.close()
                self.assertEqual(holder.wait(timeout=5), 0)

    def test_root_helper_recovery_uses_exclusive_gate_and_explicit_artifact_removal(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        recovery_gate = helper.split("with_recovery_gate() {", 1)[1].split("\n}\n", 1)[0]
        recovery = helper.split("recover_registered_process_runs() {", 1)[1].split("\n}\n\nrequire_root", 1)[0]
        recovery_helpers = helper.split("ensure_recovery_run_registry() {", 1)[1].split("\nrequire_root", 1)[0]
        remover = helper.split("remove_recovery_run_artifacts() {", 1)[1].split("\n}\n", 1)[0]

        self.assertIn('flock -n -x 9', recovery_gate)
        self.assertIn('return 75', recovery_gate)
        self.assertIn('recovery blocked by active discovery or maintenance gate', recovery_gate)
        self.assertIn('with_discovery_gate signal_registered_process_run "$@"', helper)
        self.assertIn('with_recovery_gate recover_registered_process_runs', helper)
        self.assertIn('recovery_validate_registry_layout || fail', recovery)
        self.assertNotIn('rm -rf', recovery)
        self.assertNotIn('rm -rf', recovery_helpers)
        self.assertIn('recovery_ready_pid_is_safe_to_forget "$ready_file" || return 2', remover)
        self.assertIn('rm -f --', remover)
        self.assertIn('rmdir --', remover)
        self.assertLess(remover.index('rm -f -- "$ready_file"'), remover.index('rm -f -- "$record"'))

    def test_root_helper_validates_existing_run_registry_before_reusing_it(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")
        for function_name in ("ensure_run_registry", "ensure_recovery_run_registry"):
            function = helper.split(f"{function_name}() {{", 1)[1].split("\n}\n", 1)[0]
            self.assertIn('[ ! -L "$RUN_REGISTRY_DIR" ]', function)
            self.assertIn('[ -d "$RUN_REGISTRY_DIR" ] && [ ! -L "$RUN_REGISTRY_DIR" ]', function)
            self.assertIn("'0:0:750'", function)

    def test_recover_runs_removes_valid_dead_paired_and_recordless_locks_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        dead_pid = "999999"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            run_id = "dead-paired"
            lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=dead_pid, go_pid=dead_pid, status=7)
            record = self._write_recovery_record(registry, run_id, dead_pid, "101")

            completed = self._run_recovery_with_identity_shims(shell=shell, helper=helper, root=root, registry=registry)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(record.exists())
            self.assertFalse(lock_dir.exists())

            recordless_id = "dead-recordless"
            recordless_lock = self._write_recovery_lock(
                registry, recordless_id, ready_pid=dead_pid, go_pid=dead_pid, status=0
            )
            completed = self._run_recovery_with_identity_shims(shell=shell, helper=helper, root=root, registry=registry)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(recordless_lock.exists())

            v1_recordless_lock = self._write_recovery_lock(registry, "dead-recordless-v1", ready_pid=dead_pid)
            completed = self._run_recovery_with_identity_shims(shell=shell, helper=helper, root=root, registry=registry)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(v1_recordless_lock.exists())

    def test_recover_run_requires_the_exact_paired_v2_artifacts_and_emits_a_receipt_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        dead_pid = "999999"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            helper_copy, fake_bin, _gate = self._prepare_recovery_identity_test(root, helper)
            env = self._recovery_identity_env(fake_bin, registry, root)
            run_id = "quarantined-run"

            missing = subprocess.run(
                [shell, _posix_shell_path(helper_copy), "recover-run", run_id],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 126, missing.stderr)
            self.assertIn("quarantined run recovery artifacts are missing", missing.stderr)

            lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=dead_pid, go_pid=dead_pid, status=7)
            record = self._write_recovery_record(registry, run_id, dead_pid, "101")
            completed = subprocess.run(
                [shell, _posix_shell_path(helper_copy), "recover-run", run_id],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, f"recovered-run-v1 {run_id}\n")
            self.assertFalse(record.exists())
            self.assertFalse(lock_dir.exists())

            v1_run_id = "quarantined-v1"
            v1_lock = self._write_recovery_lock(registry, v1_run_id, ready_pid=dead_pid)
            v1_record = self._write_recovery_record(registry, v1_run_id, dead_pid, "101")
            v1 = subprocess.run(
                [shell, _posix_shell_path(helper_copy), "recover-run", v1_run_id],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(v1.returncode, 126, v1.stderr)
            self.assertIn("run lock attestation is invalid", v1.stderr)
            self.assertTrue(v1_record.exists())
            self.assertTrue(v1_lock.exists())

    def test_root_helper_emits_pending_only_for_valid_pre_go_lifecycle_locks_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            helper_copy, fake_bin, _gate = self._prepare_recovery_identity_test(root, helper)
            env = self._recovery_identity_env(fake_bin, registry, root)
            run_id = "pre-go-run"
            lock_dir = registry / f".{run_id}.lock"

            for phase, ready_contents, expected_error in (
                ("v1", "helper-ready-v1 999999\n", "root run attestation is pending"),
                ("v2-before-go", "helper-ready-v2 999999 999999 101\n", "root run attestation is pending"),
                ("malformed", "helper-ready-v9 999999\n", "run lock is unsafe"),
            ):
                with self.subTest(phase=phase):
                    lock_dir.mkdir()
                    lock_dir.chmod(0o700)
                    signal_gate = lock_dir / "signal-gate"
                    signal_gate.touch()
                    signal_gate.chmod(0o600)
                    ready = lock_dir / "supervisor-ready"
                    ready.write_text(ready_contents, encoding="utf-8")
                    ready.chmod(0o600)
                    try:
                        completed = subprocess.run(
                            [shell, _posix_shell_path(helper_copy), "signal-run", run_id, "TERM"],
                            env=env,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(completed.returncode, 126, completed.stderr)
                        self.assertIn(expected_error, completed.stderr)
                    finally:
                        shutil.rmtree(lock_dir)

    def test_recover_runs_blocks_before_inspecting_an_early_phase_lock_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            early_lock = registry / ".early-phase.lock"
            early_lock.mkdir()
            helper_copy, fake_bin, gate = self._prepare_recovery_identity_test(root, helper)
            ready = root / "shared-holder-ready"
            holder = subprocess.Popen(
                [
                    shell,
                    "-c",
                    'exec 9<> "$1"; flock -s 9; : > "$2"; IFS= read -r release',
                    "shared-discovery-holder",
                    _posix_shell_path(gate),
                    _posix_shell_path(ready),
                ],
                stdin=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_path(ready)
                completed = subprocess.run(
                    [shell, _posix_shell_path(helper_copy), "recover-runs"],
                    env=self._recovery_identity_env(fake_bin, registry, root),
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(completed.returncode, 75, completed.stderr)
                self.assertIn("recovery blocked by active discovery", completed.stderr)
                self.assertTrue(early_lock.is_dir())
                self.assertEqual(list(early_lock.iterdir()), [])
            finally:
                assert holder.stdin is not None
                holder.stdin.write("release\n")
                holder.stdin.close()
                self.assertEqual(holder.wait(timeout=5), 0)

    def test_recover_runs_keeps_recordless_lock_when_ready_pid_is_live_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            ready_pid = "999999"
            lock_dir = self._write_recovery_lock(registry, "live-recordless", ready_pid=ready_pid)

            completed = self._run_recovery_with_identity_shims(
                shell=shell,
                helper=helper,
                root=root,
                registry=registry,
                extra_env={"GP_TEST_RECOVERY_PROCESS_TABLE": f"{ready_pid} {ready_pid} S\n"},
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("supervisor is still live", completed.stderr)
            self.assertTrue(lock_dir.is_dir())
            self.assertTrue((lock_dir / "supervisor-ready").is_file())

    def test_recover_runs_keeps_artifacts_when_a_live_member_survives_a_missing_or_zombie_leader_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        for layout in ("paired", "recordless"):
            for leader_state in ("absent", "zombie"):
                with self.subTest(layout=layout, leader_state=leader_state), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    registry = root / "runs"
                    registry.mkdir()
                    run_id = f"{layout}-{leader_state}-live-child"
                    lock_dir = self._write_recovery_lock(
                        registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7
                    )
                    record = None
                    if layout == "paired":
                        record = self._write_recovery_record(registry, run_id, ready_pid, "101")
                    # The production query is exactly: ps -e -o pgid= -o sid= -o stat=.
                    # It cannot report a member PID, but this row represents a non-leader
                    # child that remains in the original process group and session.
                    process_table = f"{ready_pid} {ready_pid} S\n"
                    if leader_state == "zombie":
                        process_table = f"{ready_pid} {ready_pid} Z\n{process_table}"

                    completed = self._run_recovery_with_identity_shims(
                        shell=shell,
                        helper=helper,
                        root=root,
                        registry=registry,
                        extra_env={"GP_TEST_RECOVERY_PROCESS_TABLE": process_table},
                    )

                    self.assertEqual(completed.returncode, 126, completed.stderr)
                    expected_error = (
                        f"gp-root-helper: registered process is stale or invalid: {run_id}\n"
                        if layout == "paired"
                        else f"gp-root-helper: run lock supervisor is still live: {run_id}\n"
                    )
                    self.assertEqual(completed.stderr, expected_error)
                    if record is not None:
                        self.assertTrue(record.exists())
                    self.assertTrue(lock_dir.is_dir())
                    self.assertTrue((lock_dir / "supervisor-ready").is_file())
                    self.assertTrue((lock_dir / "supervisor-go").is_file())
                    self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_removes_zombie_only_paired_and_recordless_locks_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        process_table = f"{ready_pid} {ready_pid} Z\n"
        for layout in ("paired", "recordless"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                registry = root / "runs"
                registry.mkdir()
                run_id = f"{layout}-zombie-only"
                lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7)
                record = None
                if layout == "paired":
                    record = self._write_recovery_record(registry, run_id, ready_pid, "101")

                completed = self._run_recovery_with_identity_shims(
                    shell=shell,
                    helper=helper,
                    root=root,
                    registry=registry,
                    extra_env={"GP_TEST_RECOVERY_PROCESS_TABLE": process_table},
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                if record is not None:
                    self.assertFalse(record.exists())
                self.assertFalse(lock_dir.exists())

    def test_recover_runs_accepts_unrelated_kernel_rows_and_parked_processes_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        cases = {
            "zombie-with-kernel-row": (f"0 0 S\n{ready_pid} {ready_pid} Z\n", 0),
            "parked-with-kernel-row": (f"0 0 S\n{ready_pid} {ready_pid} P\n", 126),
        }
        for case, (process_table, expected_code) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                registry = root / "runs"
                registry.mkdir()
                run_id = case
                lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7)
                record = self._write_recovery_record(registry, run_id, ready_pid, "101")

                completed = self._run_recovery_with_identity_shims(
                    shell=shell,
                    helper=helper,
                    root=root,
                    registry=registry,
                    extra_env={"GP_TEST_RECOVERY_PROCESS_TABLE": process_table},
                )

                self.assertEqual(completed.returncode, expected_code, completed.stderr)
                if expected_code == 0:
                    self.assertFalse(record.exists())
                    self.assertFalse(lock_dir.exists())
                else:
                    self.assertIn("stale or invalid", completed.stderr)
                    self.assertNotIn("cannot be safely inspected", completed.stderr)
                    self.assertTrue(record.exists())
                    self.assertTrue(lock_dir.is_dir())

    def test_recover_runs_fails_closed_when_group_inspection_is_incomplete_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        inspection_failures = {
            "empty": {"GP_TEST_RECOVERY_PROCESS_TABLE": ""},
            "whitespace": {"GP_TEST_RECOVERY_PROCESS_TABLE": " \t\n"},
            "malformed": {"GP_TEST_RECOVERY_PROCESS_TABLE": f"{ready_pid} {ready_pid}\n"},
            "nonzero": {"GP_TEST_RECOVERY_PS_STATUS": "1"},
        }
        for layout in ("paired", "recordless"):
            for case, extra_env in inspection_failures.items():
                with self.subTest(layout=layout, case=case), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    registry = root / "runs"
                    registry.mkdir()
                    run_id = f"{layout}-{case}-inspection"
                    lock_dir = self._write_recovery_lock(
                        registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7
                    )
                    record = None
                    if layout == "paired":
                        record = self._write_recovery_record(registry, run_id, ready_pid, "101")

                    completed = self._run_recovery_with_identity_shims(
                        shell=shell,
                        helper=helper,
                        root=root,
                        registry=registry,
                        extra_env=extra_env,
                    )

                    self.assertEqual(completed.returncode, 126, completed.stderr)
                    self.assertIn("supervisor cannot be safely inspected", completed.stderr)
                    if record is not None:
                        self.assertTrue(record.exists())
                    self.assertTrue(lock_dir.is_dir())
                    self.assertTrue((lock_dir / "supervisor-ready").is_file())
                    self.assertTrue((lock_dir / "supervisor-go").is_file())
                    self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_preserves_paired_artifacts_when_registered_identity_is_unavailable_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None or not Path(f"/proc/{os.getpid()}/stat").is_file():
            self.skipTest("requires a POSIX shell with flock and procfs")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = str(os.getpid())
        marker = Path(f"/proc/{ready_pid}/stat").read_text(encoding="utf-8").split()[21]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            run_id = "registered-identity-unavailable"
            lock_dir = self._write_recovery_lock(
                registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7, marker=marker
            )
            record = self._write_recovery_record(registry, run_id, ready_pid, marker)

            completed = self._run_recovery_with_identity_shims(
                shell=shell,
                helper=helper,
                root=root,
                registry=registry,
                extra_env={"GP_TEST_RECOVERY_MANAGED_PGID_PS_STATUS": "1"},
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("registered process cannot be safely inspected", completed.stderr)
            self.assertTrue(record.exists())
            self.assertTrue(lock_dir.is_dir())
            self.assertTrue((lock_dir / "supervisor-ready").is_file())
            self.assertTrue((lock_dir / "supervisor-go").is_file())
            self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_fails_closed_on_malformed_group_zombie_stat_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        for layout in ("paired", "recordless"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                registry = root / "runs"
                registry.mkdir()
                run_id = f"{layout}-malformed-group-zombie"
                lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7)
                record = (
                    self._write_recovery_record(registry, run_id, ready_pid, "101")
                    if layout == "paired"
                    else None
                )
                ps_query_log = root / "ps-query-log"

                completed = self._run_recovery_with_identity_shims(
                    shell=shell,
                    helper=helper,
                    root=root,
                    registry=registry,
                    extra_env={
                        "GP_TEST_RECOVERY_PROCESS_TABLE": f"{ready_pid} {ready_pid} Zbad\n",
                        "GP_TEST_RECOVERY_PS_QUERY_LOG": _posix_shell_path(ps_query_log),
                    },
                )

                self.assertEqual(completed.returncode, 126, completed.stderr)
                self.assertIn("cannot be safely inspected", completed.stderr)
                self.assertEqual(ps_query_log.read_text(encoding="utf-8").splitlines(), ["-e -o pgid= -o sid= -o stat="])
                if record is not None:
                    self.assertTrue(record.exists())
                self.assertTrue(lock_dir.is_dir())
                self.assertTrue((lock_dir / "supervisor-ready").is_file())
                self.assertTrue((lock_dir / "supervisor-go").is_file())
                self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_validates_leader_specific_zombie_inspection_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None or not Path(f"/proc/{os.getpid()}/stat").is_file():
            self.skipTest("requires a POSIX shell with procfs")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = str(os.getpid())
        marker = Path(f"/proc/{ready_pid}/stat").read_text(encoding="utf-8").split()[21]
        leader_query = f"-o pgid= -o sid= -o stat= -p {ready_pid}"
        cases = {
            "empty-group": (f"{ready_pid} {ready_pid} Z\n", f"{ready_pid} {ready_pid} Z+\n", 0),
            "live-group-member": (
                f"{ready_pid} {ready_pid} Z\n{ready_pid} {ready_pid} S\n",
                f"{ready_pid} {ready_pid} Z+\n",
                126,
            ),
            "malformed-leader": (f"{ready_pid} {ready_pid} Z\n", f"{ready_pid} {ready_pid} Zbad\n", 126),
            "nonzero-leader": (f"{ready_pid} {ready_pid} Z\n", "", 126),
        }
        for layout in ("paired", "recordless"):
            for case, (process_table, leader_output, expected_code) in cases.items():
                with self.subTest(layout=layout, case=case), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    registry = root / "runs"
                    registry.mkdir()
                    run_id = f"{layout}-{case}-leader-zombie"
                    lock_dir = self._write_recovery_lock(
                        registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7, marker=marker
                    )
                    record = self._write_recovery_record(registry, run_id, ready_pid, marker) if layout == "paired" else None
                    ps_query_log = root / "ps-query-log"
                    extra_env = {
                        "GP_TEST_RECOVERY_PROCESS_TABLE": process_table,
                        "GP_TEST_RECOVERY_LEADER_PROCESS_TABLE": leader_output,
                        "GP_TEST_RECOVERY_PS_QUERY_LOG": _posix_shell_path(ps_query_log),
                    }
                    if case == "nonzero-leader":
                        extra_env["GP_TEST_RECOVERY_LEADER_PS_STATUS"] = "1"

                    completed = self._run_recovery_with_identity_shims(
                        shell=shell,
                        helper=helper,
                        root=root,
                        registry=registry,
                        extra_env=extra_env,
                    )

                    self.assertEqual(completed.returncode, expected_code, completed.stderr)
                    queries = ps_query_log.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(queries[0], "-e -o pgid= -o sid= -o stat=")
                    if case == "live-group-member":
                        self.assertEqual(queries, ["-e -o pgid= -o sid= -o stat="])
                    else:
                        self.assertIn(leader_query, queries)
                    if expected_code == 0:
                        if record is not None:
                            self.assertFalse(record.exists())
                        self.assertFalse(lock_dir.exists())
                    else:
                        if layout == "paired" and case == "live-group-member":
                            self.assertEqual(
                                completed.stderr,
                                f"gp-root-helper: registered process is stale or invalid: {run_id}\n",
                            )
                        else:
                            self.assertIn("supervisor", completed.stderr)
                        if record is not None:
                            self.assertTrue(record.exists())
                        self.assertTrue(lock_dir.is_dir())
                        self.assertTrue((lock_dir / "supervisor-ready").is_file())
                        self.assertTrue((lock_dir / "supervisor-go").is_file())
                        self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_revalidates_liveness_before_deleting_paired_artifacts_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            run_id = "liveness-changes-before-removal"
            lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7)
            record = self._write_recovery_record(registry, run_id, ready_pid, "101")
            ps_call_count = root / "ps-call-count"
            ps_call_count.write_text("0\n", encoding="utf-8")

            completed = self._run_recovery_with_identity_shims(
                shell=shell,
                helper=helper,
                root=root,
                registry=registry,
                extra_env={
                    "GP_TEST_RECOVERY_PS_CALL_COUNT_PATH": _posix_shell_path(ps_call_count),
                    "GP_TEST_RECOVERY_FIRST_PROCESS_TABLE": f"{ready_pid} {ready_pid} Z\n",
                    "GP_TEST_RECOVERY_NEXT_PROCESS_TABLE": f"{ready_pid} {ready_pid} S\n",
                },
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("artifacts changed or cannot be safely inspected", completed.stderr)
            self.assertEqual(ps_call_count.read_text(encoding="utf-8"), "2\n")
            self.assertTrue(record.exists())
            self.assertTrue(lock_dir.is_dir())
            self.assertTrue((lock_dir / "supervisor-ready").is_file())
            self.assertTrue((lock_dir / "supervisor-go").is_file())
            self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_rejects_recordless_v2_marker_changes_before_removal_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            run_id = "recordless-marker-changes-before-removal"
            lock_dir = self._write_recovery_lock(
                registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7, marker="101"
            )
            ready_file = lock_dir / "supervisor-ready"

            completed = self._run_recovery_with_identity_shims(
                shell=shell,
                helper=helper,
                root=root,
                registry=registry,
                extra_env={
                    # A valid unrelated row proves this group absent; empty
                    # ps output deliberately fails closed before the race.
                    "GP_TEST_RECOVERY_PROCESS_TABLE": "1 1 S\n",
                    "GP_TEST_RECOVERY_TAMPER_READY_PATH": _posix_shell_path(ready_file),
                    "GP_TEST_RECOVERY_TAMPER_READY_CONTENT": f"helper-ready-v2 {ready_pid} {ready_pid} 202\n",
                },
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertEqual(
                completed.stderr,
                f"gp-root-helper: recovered run lock changed or cannot be safely inspected: {run_id}\n",
            )
            self.assertEqual(ready_file.read_text(encoding="utf-8"), f"helper-ready-v2 {ready_pid} {ready_pid} 202\n")
            self.assertTrue(lock_dir.is_dir())
            self.assertTrue((lock_dir / "supervisor-go").is_file())
            self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_rejects_record_appearing_before_recordless_removal_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        ready_pid = "999999"
        marker = "101"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            run_id = "record-appears-before-recordless-removal"
            lock_dir = self._write_recovery_lock(
                registry, run_id, ready_pid=ready_pid, go_pid=ready_pid, status=7, marker=marker
            )
            record = registry / run_id

            completed = self._run_recovery_with_identity_shims(
                shell=shell,
                helper=helper,
                root=root,
                registry=registry,
                extra_env={
                    "GP_TEST_RECOVERY_PROCESS_TABLE": "1 1 S\n",
                    "GP_TEST_RECOVERY_APPEAR_RECORD_PATH": _posix_shell_path(record),
                    "GP_TEST_RECOVERY_APPEAR_RECORD_CONTENT": f"helper-v1 {ready_pid} {ready_pid} {marker}\n",
                },
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertEqual(
                completed.stderr,
                f"gp-root-helper: recovered run lock changed or cannot be safely inspected: {run_id}\n",
            )
            self.assertEqual(record.read_text(encoding="utf-8"), f"helper-v1 {ready_pid} {ready_pid} {marker}\n")
            self.assertTrue(lock_dir.is_dir())
            self.assertTrue((lock_dir / "supervisor-ready").is_file())
            self.assertTrue((lock_dir / "supervisor-go").is_file())
            self.assertTrue((lock_dir / "target-status").is_file())

    def test_recover_runs_leaves_suspicious_artifacts_untouched_portably(self) -> None:
        shell = _posix_shell()
        if shell is None or shutil.which("flock") is None:
            self.skipTest("requires a POSIX shell with flock")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        dead_pid = "999999"
        cases = (
            "unexpected-hidden",
            "nested-layout",
            "lifecycle-directory",
            "lifecycle-symlink",
            "bad-mode",
            "bad-owner",
            "bad-ready-content",
            "ready-go-pid-mismatch",
            "status-before-go",
            "bad-record-content",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                registry = root / "runs"
                registry.mkdir()
                run_id = f"suspicious-{case}"
                record: Path | None = None
                extra_env: dict[str, str] = {}

                if case == "lifecycle-directory":
                    lock_dir = registry / f".{run_id}.lock"
                    lock_dir.mkdir()
                    (lock_dir / "supervisor-ready").mkdir()
                elif case == "lifecycle-symlink":
                    lock_dir = registry / f".{run_id}.lock"
                    lock_dir.mkdir()
                    target = root / "ready-target"
                    target.write_text(f"helper-ready-v1 {dead_pid}\n", encoding="utf-8")
                    try:
                        (lock_dir / "supervisor-ready").symlink_to(target)
                    except OSError as exc:
                        self.skipTest(f"requires symlink support: {exc}")
                elif case == "bad-ready-content":
                    lock_dir = registry / f".{run_id}.lock"
                    lock_dir.mkdir()
                    (lock_dir / "supervisor-ready").write_text("not-a-ready-record\n", encoding="utf-8")
                elif case == "ready-go-pid-mismatch":
                    lock_dir = self._write_recovery_lock(
                        registry, run_id, ready_pid=dead_pid, go_pid="999998"
                    )
                elif case == "status-before-go":
                    lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=dead_pid, status=7)
                elif case == "bad-record-content":
                    lock_dir = self._write_recovery_lock(
                        registry, run_id, ready_pid=dead_pid, go_pid=dead_pid, status=7
                    )
                    record = registry / run_id
                    record.write_text("not-a-run-record\n", encoding="utf-8")
                    record.chmod(0o600)
                else:
                    lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=dead_pid)
                    if case == "unexpected-hidden":
                        (lock_dir / ".unexpected").write_text("keep\n", encoding="utf-8")
                    elif case == "nested-layout":
                        (lock_dir / "nested").mkdir()
                    elif case == "bad-mode":
                        extra_env = {
                            "GP_TEST_RECOVERY_BAD_STAT_PATH": _posix_shell_path(lock_dir / "supervisor-ready"),
                            "GP_TEST_RECOVERY_BAD_STAT_VALUE": "0:0:644",
                        }
                    elif case == "bad-owner":
                        extra_env = {
                            "GP_TEST_RECOVERY_BAD_STAT_PATH": _posix_shell_path(lock_dir / "supervisor-ready"),
                            "GP_TEST_RECOVERY_BAD_STAT_VALUE": "1:0:600",
                        }

                completed = self._run_recovery_with_identity_shims(
                    shell=shell,
                    helper=helper,
                    root=root,
                    registry=registry,
                    extra_env=extra_env,
                )

                self.assertEqual(completed.returncode, 126, completed.stderr)
                self.assertTrue(lock_dir.is_dir())
                if record is not None:
                    self.assertTrue(record.exists())

    def test_root_recover_runs_removes_valid_dead_artifacts_with_real_metadata(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("flock"):
            self.skipTest("requires a root Linux test environment with flock")
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX shell")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        dead_pid = "999999"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir(mode=0o750)
            registry.chmod(0o750)
            helper_copy = root / "gp-root-helper-recovery-real.sh"
            helper_copy.write_text(
                _root_helper_test_source(helper).replace(
                    "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                    f"DISCOVERY_GATE_DIR='{_posix_shell_path(root / 'gates')}'",
                ),
                encoding="utf-8",
            )
            run_id = "real-dead-paired"
            lock_dir = self._write_recovery_lock(registry, run_id, ready_pid=dead_pid, go_pid=dead_pid, status=7)
            record = self._write_recovery_record(registry, run_id, dead_pid, "101")
            env = {
                **os.environ,
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
                "GP_ROOT_HELPER_CONFIG": str(root / "missing-config"),
            }

            completed = subprocess.run(
                [shell, str(helper_copy), "recover-runs"], env=env, text=True, capture_output=True, check=False
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(record.exists())
            self.assertFalse(lock_dir.exists())

            recordless_lock = self._write_recovery_lock(registry, "real-dead-recordless", ready_pid=dead_pid)
            completed = subprocess.run(
                [shell, str(helper_copy), "recover-runs"], env=env, text=True, capture_output=True, check=False
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(recordless_lock.exists())

    def _write_recovery_lock(
        self,
        registry: Path,
        run_id: str,
        *,
        ready_pid: str,
        go_pid: str | None = None,
        status: int | None = None,
        marker: str = "101",
    ) -> Path:
        lock_dir = registry / f".{run_id}.lock"
        lock_dir.mkdir()
        lock_dir.chmod(0o700)
        lifecycle_gate = lock_dir / "signal-gate"
        lifecycle_gate.touch()
        lifecycle_gate.chmod(0o600)
        ready_file = lock_dir / "supervisor-ready"
        ready_value = (
            f"helper-ready-v2 {ready_pid} {ready_pid} {marker}\n"
            if go_pid is not None
            else f"helper-ready-v1 {ready_pid}\n"
        )
        ready_file.write_text(ready_value, encoding="utf-8")
        ready_file.chmod(0o600)
        if go_pid is not None:
            go_file = lock_dir / "supervisor-go"
            go_file.write_text(f"helper-go-v1 {go_pid}\n", encoding="utf-8")
            go_file.chmod(0o600)
        if status is not None:
            status_file = lock_dir / "target-status"
            status_file.write_text(f"helper-status-v1 {status}\n", encoding="utf-8")
            status_file.chmod(0o600)
        return lock_dir

    def _write_recovery_record(self, registry: Path, run_id: str, pid: str, marker: str) -> Path:
        record = registry / run_id
        record.write_text(f"helper-v1 {pid} {pid} {marker}\n", encoding="utf-8")
        record.chmod(0o600)
        return record

    def _prepare_recovery_identity_test(self, root: Path, helper: Path) -> tuple[Path, Path, Path]:
        fake_bin = root / "recovery-fake-bin"
        fake_bin.mkdir(exist_ok=True)
        self._write_recovery_identity_shims(fake_bin)
        gate_dir = root / "gates"
        gate_dir.mkdir(exist_ok=True)
        gate = gate_dir / "discovery-update.lock"
        gate.touch()
        helper_copy = root / "gp-root-helper-recovery-test.sh"
        helper_copy.write_text(
            _root_helper_test_source(helper).replace(
                "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                f"DISCOVERY_GATE_DIR='{_posix_shell_path(gate_dir)}'",
            ),
            encoding="utf-8",
        )
        return helper_copy, fake_bin, gate

    def _write_recovery_identity_shims(self, fake_bin: Path) -> None:
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
        (fake_bin / "install").write_text(
            "#!/bin/sh\nfor destination do :; done\n[ -d \"$destination\" ] || mkdir -p \"$destination\"\n",
            encoding="utf-8",
        )
        (fake_bin / "stat").write_text(
            "#!/bin/sh\n"
            "if [ -n \"${GP_TEST_RECOVERY_BAD_STAT_PATH:-}\" ]; then\n"
            "  case \"$*\" in\n"
            "    *\"$GP_TEST_RECOVERY_BAD_STAT_PATH\"*) printf '%s\\n' \"$GP_TEST_RECOVERY_BAD_STAT_VALUE\"; exit 0 ;;\n"
            "  esac\n"
            "fi\n"
            "case \"$*\" in\n"
            "  *discovery-update.lock*) printf '0:0:600\\n' ;;\n"
            "  */gates) printf '0:0:700\\n' ;;\n"
            "  */runs) printf '0:0:750\\n' ;;\n"
            "  *.lock) printf '0:0:700\\n' ;;\n"
            "  *) printf '0:0:600\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "ps").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  '-e -o pgid= -o sid= -o stat=')\n"
            "    if [ -n \"${GP_TEST_RECOVERY_TAMPER_READY_PATH:-}\" ]; then\n"
            "      printf '%s' \"$GP_TEST_RECOVERY_TAMPER_READY_CONTENT\" > \"$GP_TEST_RECOVERY_TAMPER_READY_PATH\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_APPEAR_RECORD_PATH:-}\" ]; then\n"
            "      printf '%s' \"$GP_TEST_RECOVERY_APPEAR_RECORD_CONTENT\" > \"$GP_TEST_RECOVERY_APPEAR_RECORD_PATH\"\n"
            "      chmod 600 \"$GP_TEST_RECOVERY_APPEAR_RECORD_PATH\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_PS_QUERY_LOG:-}\" ]; then\n"
            "      printf '%s\\n' \"$*\" >> \"$GP_TEST_RECOVERY_PS_QUERY_LOG\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_PS_STATUS:-}\" ]; then\n"
            "      exit \"$GP_TEST_RECOVERY_PS_STATUS\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_PS_CALL_COUNT_PATH:-}\" ]; then\n"
            "      count=$(cat \"$GP_TEST_RECOVERY_PS_CALL_COUNT_PATH\")\n"
            "      count=$((count + 1))\n"
            "      printf '%s\\n' \"$count\" > \"$GP_TEST_RECOVERY_PS_CALL_COUNT_PATH\"\n"
            "      if [ \"$count\" -eq 1 ]; then\n"
            "        printf '%s' \"${GP_TEST_RECOVERY_FIRST_PROCESS_TABLE:-}\"\n"
            "      else\n"
            "        printf '%s' \"${GP_TEST_RECOVERY_NEXT_PROCESS_TABLE:-}\"\n"
            "      fi\n"
            "      exit 0\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_PROCESS_TABLE+x}\" ]; then\n"
            "      printf '%s' \"$GP_TEST_RECOVERY_PROCESS_TABLE\"\n"
            "      exit 0\n"
            "    fi\n"
            "    ;;\n"
            "  '-o pgid= -p '*)\n"
            "    if [ -n \"${GP_TEST_RECOVERY_MANAGED_PGID_PS_STATUS:-}\" ]; then\n"
            "      exit \"$GP_TEST_RECOVERY_MANAGED_PGID_PS_STATUS\"\n"
            "    fi\n"
            "    ;;\n"
            "  '-o pgid= -o sid= -o stat= -p '*)\n"
            "    if [ -n \"${GP_TEST_RECOVERY_PS_QUERY_LOG:-}\" ]; then\n"
            "      printf '%s\\n' \"$*\" >> \"$GP_TEST_RECOVERY_PS_QUERY_LOG\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_LEADER_PS_STATUS:-}\" ]; then\n"
            "      exit \"$GP_TEST_RECOVERY_LEADER_PS_STATUS\"\n"
            "    fi\n"
            "    if [ -n \"${GP_TEST_RECOVERY_LEADER_PROCESS_TABLE+x}\" ]; then\n"
            "      printf '%s' \"$GP_TEST_RECOVERY_LEADER_PROCESS_TABLE\"\n"
            "      exit 0\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
            "exec /usr/bin/ps \"$@\"\n",
            encoding="utf-8",
        )
        for shim in fake_bin.iterdir():
            shim.chmod(0o700)

    def _recovery_identity_env(
        self, fake_bin: Path, registry: Path, root: Path, extra_env: dict[str, str] | None = None
    ) -> dict[str, str]:
        return {
            **os.environ,
            **(extra_env or {}),
            "PATH": f"{_posix_shell_path(fake_bin)}:/usr/bin:/bin",
            "GP_ROOT_HELPER_RUN_DIR": _posix_shell_path(registry),
            "GP_ROOT_HELPER_CONFIG": _posix_shell_path(root / "missing-config"),
        }

    def _run_recovery_with_identity_shims(
        self,
        *,
        shell: str,
        helper: Path,
        root: Path,
        registry: Path,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        helper_copy, fake_bin, _gate = self._prepare_recovery_identity_test(root, helper)
        return subprocess.run(
            [shell, _posix_shell_path(helper_copy), "recover-runs"],
            env=self._recovery_identity_env(fake_bin, registry, root, extra_env),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_root_helper_cleanup_is_limited_to_a_validated_isolated_process_group(self) -> None:
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")

        self.assertIn('process_session_id() {', helper)
        self.assertIn('is_valid_ps_identifier() {', helper)
        self.assertIn('0) return 0 ;;', helper)
        self.assertIn('D|I|P|R|S|T|t|W|X|Z', helper)
        self.assertIn('[ "$listed_pgid" = "$known_pgid" ] &&', helper)
        self.assertIn('[ "$listed_sid" = "$known_pid" ]', helper)
        self.assertIn('terminate_known_process_group "$pid" "$pgid" "$marker" "$signal"', helper)
        self.assertIn('terminate_known_process_group "$pid" "$pgid" "$marker" TERM', helper)
        self.assertIn('for known_kill_binary in /bin/kill /usr/bin/kill; do', helper)
        self.assertIn('"$known_kill_binary" "-$known_signal" -- "-$known_pgid"', helper)
        self.assertIn('rm -f -- "$record"', helper)
        signal_handler = helper.split("signal_registered_process_run() {", 1)[1].split(
            "\n}\n\nensure_recovery_run_registry() {", 1
        )[0]
        self.assertLess(
            signal_handler.index('terminate_known_process_group "$pid" "$pgid" "$marker" "$signal"'),
            signal_handler.rindex('rm -f -- "$record"'),
        )

    def test_process_start_time_parser_uses_field_22_after_the_final_comm_delimiter(self) -> None:
        if sys.platform == "linux":
            dash = Path("/bin/dash")
            if not dash.is_file():
                self.skipTest("requires /bin/dash for the POSIX parser fixture on Linux")
            shell = str(dash)
        else:
            shell = _posix_shell()
            if shell is None:
                self.skipTest("requires a POSIX sh interpreter")
        parser_env = dict(os.environ)
        if os.name == "nt":
            git_usr_bin = Path(r"C:\Program Files\Git\usr\bin")
            if not (git_usr_bin / "awk.exe").is_file():
                self.skipTest("requires awk for the POSIX parser fixture")
            parser_env["PATH"] = f"{git_usr_bin}{os.pathsep}{parser_env.get('PATH', '')}"
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "root-helper-library.sh"
            library.write_text(_root_helper_test_source(helper).split("\nrequire_root\n", 1)[0] + "\n", encoding="utf-8")
            stat_fields = ["S", *map(str, range(1, 20))]
            stat_fields[19] = "197364730"
            for name, comm in (("simple", "sh"), ("spaces-and-parens", "worker ) name)")):
                fixture = root / name
                fixture.write_text(f"7187 ({comm}) {' '.join(stat_fields)}\n", encoding="utf-8")
                completed = subprocess.run(
                    [shell, "-c", '. "$1"; process_start_time_from_stat "$2"', "root-helper-parser", str(library), str(fixture)],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=parser_env,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "197364730\n")

            missing_stat_fixture = root / "missing-stat"
            completed = subprocess.run(
                [
                    shell,
                    "-c",
                    '''
awk() {
  printf '%s\n' 'unexpected awk invocation' >&2
  return 0
}
. "$1"
process_start_time_from_stat "$2"
''',
                    "root-helper-missing-stat",
                    str(library),
                    str(missing_stat_fixture),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=parser_env,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertNotIn("unexpected awk invocation", completed.stderr)

            invalid_fixtures = {
                "missing-delimiter": f"7187 sh {' '.join(stat_fields)}\n",
                "multiline": f"7187 (sh) {' '.join(stat_fields)}\nextra\n",
                "non-numeric": f"7187 (sh) {' '.join([*stat_fields[:19], 'not-a-marker'])}\n",
            }
            for name, contents in invalid_fixtures.items():
                fixture = root / name
                fixture.write_text(contents, encoding="utf-8")
                completed = subprocess.run(
                    [shell, "-c", '. "$1"; process_start_time_from_stat "$2"', "root-helper-parser", str(library), str(fixture)],
                    text=True,
                    capture_output=True,
                    check=False,
                    env=parser_env,
                )
                self.assertEqual(completed.returncode, 2, name)
            completed = subprocess.run(
                [shell, "-c", '. "$1"; process_start_time_from_stat "$2"', "root-helper-parser", str(library), str(root / "missing")],
                text=True,
                capture_output=True,
                check=False,
                env=parser_env,
            )
            self.assertEqual(completed.returncode, 2)

    def test_signal_run_preserves_record_and_skips_signal_when_identity_inspection_is_unavailable(self) -> None:
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX shell with procfs")
        if subprocess.run([shell, "-c", "[ -r /proc/$$/stat ]"], check=False).returncode != 0:
            self.skipTest("requires a POSIX shell with procfs")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            gate_dir = root / "gates"
            gate_dir.mkdir()
            helper_copy = root / "gp-root-helper-unsafe-signal-test.sh"
            helper_copy.write_text(
                _root_helper_test_source(helper).replace(
                    "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                    f"DISCOVERY_GATE_DIR='{_posix_shell_path(gate_dir)}'",
                ),
                encoding="utf-8",
            )
            signal_log = root / "signals.log"
            (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
            (fake_bin / "install").write_text(
                "#!/bin/sh\nfor destination do :; done\nmkdir -p \"$destination\"\n", encoding="utf-8"
            )
            (fake_bin / "stat").write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *discovery-update.lock*) printf '0:0:600\\n' ;;\n"
                "  */gates) printf '0:0:700\\n' ;;\n"
                "  */runs) printf '0:0:750\\n' ;;\n"
                "  *.lock) printf '0:0:700\\n' ;;\n"
                "  *) printf '0:0:600\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "chmod").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "ps").write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'-o pgid= -p '*) exit 1 ;;\n"
                "esac\n"
                "exec /usr/bin/ps \"$@\"\n",
                encoding="utf-8",
            )
            (fake_bin / "awk").write_text("#!/bin/sh\nprintf '101\\n'\n", encoding="utf-8")
            for shim in fake_bin.iterdir():
                shim.chmod(0o700)

            harness = """
fake_bin=$1
registry=$2
signal_log=$3
helper=$4
PATH="$fake_bin:/usr/bin:/bin"
GP_ROOT_HELPER_RUN_DIR="$registry"
pid=$$
printf 'helper-v1 %s %s 101\n' "$pid" "$pid" > "$registry/unsafe-identity"
lock_dir="$registry/.unsafe-identity.lock"
mkdir "$lock_dir"
printf 'helper-ready-v2 %s %s 101\n' "$pid" "$pid" > "$lock_dir/supervisor-ready"
printf 'helper-go-v1 %s\n' "$pid" > "$lock_dir/supervisor-go"
: > "$lock_dir/signal-gate"
export PATH GP_ROOT_HELPER_RUN_DIR
kill() { printf '%s\n' "$*" >> "$signal_log"; }
set -- signal-run unsafe-identity TERM
. "$helper"
"""
            completed = subprocess.run(
                [
                    shell,
                    "-c",
                    harness,
                    "root-helper-unsafe-signal",
                    _posix_shell_path(fake_bin),
                    _posix_shell_path(registry),
                    _posix_shell_path(signal_log),
                    _posix_shell_path(helper_copy),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("registered process cannot be safely inspected", completed.stderr)
            self.assertTrue((registry / "unsafe-identity").is_file())
            self.assertFalse(signal_log.exists())

    def test_root_helper_does_not_signal_when_marker_changes_at_revalidation_boundary(self) -> None:
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter to execute the root-helper")
        if subprocess.run([shell, "-c", "[ -r /proc/$$/stat ]"], check=False).returncode != 0:
            self.skipTest("requires the /proc start-marker interface used by the root-helper")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            (root / "gates").mkdir()
            helper_copy = root / "gp-root-helper-marker-test.sh"
            helper_copy.write_text(
                _root_helper_test_source(helper).replace(
                    "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                    f"DISCOVERY_GATE_DIR='{_posix_shell_path(root / 'gates')}'",
                ).replace(
                    "\nrequire_root\n",
                    '\nsignal_known_process_group() { kill "-$1" -- "-$2"; }\n\nrequire_root\n',
                ),
                encoding="utf-8",
            )
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            marker_state = root / "marker-count"
            signal_log = root / "signals.log"
            self._write_root_helper_marker_shims(fake_bin)

            for helper_command, run_id, valid_markers in (
                # The third match is the last revalidation immediately before TERM.
                ("signal-run", "marker-stale-before-term", 2),
                ("recover-runs", "marker-stale-during-recovery", 0),
            ):
                with self.subTest(command=helper_command, run_id=run_id):
                    marker_state.write_text("0", encoding="utf-8")
                    signal_log.unlink(missing_ok=True)
                    lock_dir = registry / f".{run_id}.lock"
                    status_file = lock_dir / "target-status"
                    completed = self._run_root_helper_with_marker_shims(
                        shell=shell,
                        helper=helper_copy,
                        fake_bin=fake_bin,
                        registry=registry,
                        marker_state=marker_state,
                        signal_log=signal_log,
                        run_id=run_id,
                        valid_markers=valid_markers,
                        helper_command=helper_command,
                    )

                    if helper_command == "signal-run":
                        self.assertEqual(completed.returncode, 126, completed.stderr)
                        self.assertIn("stale or invalid", completed.stderr)
                        self.assertTrue((registry / run_id).exists())
                    else:
                        self.assertEqual(completed.returncode, 126, completed.stderr)
                        self.assertIn("stale or invalid", completed.stderr)
                        self.assertTrue((registry / run_id).exists())
                    actual_signals = signal_log.read_text(encoding="utf-8").splitlines() if signal_log.exists() else []
                    self.assertEqual(actual_signals, [])
                    # A stale marker must never authorize deleting the private status/lock:
                    # it may still describe an unverified active supervisor group.
                    self.assertTrue(lock_dir.is_dir())
                    self.assertEqual(status_file.read_text(encoding="utf-8"), "helper-status-v1 7\n")
                    shutil.rmtree(lock_dir)
                    (registry / run_id).unlink(missing_ok=True)

    def _write_root_helper_marker_shims(self, fake_bin: Path) -> None:
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
        (fake_bin / "install").write_text(
            "#!/bin/sh\nfor destination do :; done\nmkdir -p \"$destination\"\n", encoding="utf-8"
        )
        (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "chmod").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "stat").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *discovery-update.lock*) printf '0:0:600\\n' ;;\n"
            "  */gates) printf '0:0:700\\n' ;;\n"
            "  */runs) printf '0:0:750\\n' ;;\n"
            "  *.lock) printf '0:0:700\\n' ;;\n"
            "  *) printf '0:0:600\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "ps").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'-e -o pid= -o pgid= -o sid='*) printf '%s %s %s\\n' \"$FAKE_PROCESS_ID\" \"$FAKE_PROCESS_ID\" \"$FAKE_PROCESS_ID\" ;;\n"
            "  *'-e -o pgid= -o sid= -o stat='*) printf '%s %s S\\n' \"$FAKE_PROCESS_ID\" \"$FAKE_PROCESS_ID\" ;;\n"
            "  *'-e -o pgid= -o sid='*) printf '%s %s\\n' \"$FAKE_PROCESS_ID\" \"$FAKE_PROCESS_ID\" ;;\n"
            "  *'-o pgid='*) printf '%s\\n' \"$FAKE_PROCESS_ID\" ;;\n"
            "  *'-o sid='*) printf '%s\\n' \"$FAKE_PROCESS_ID\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "awk").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'-v pgid='*) cat >/dev/null; exit 0 ;;\n"
            "esac\n"
            "count=$(cat \"$FAKE_MARKER_STATE\")\n"
            "count=$((count + 1))\n"
            "printf '%s\\n' \"$count\" > \"$FAKE_MARKER_STATE\"\n"
            "if [ \"$count\" -le \"$FAKE_VALID_MARKERS\" ]; then\n"
            "  printf '101\\n'\n"
            "else\n"
            "  printf '202\\n'\n"
            "fi\n",
            encoding="utf-8",
        )
        for shim in fake_bin.iterdir():
            shim.chmod(0o700)

    def _run_root_helper_with_marker_shims(
        self,
        *,
        shell: str,
        helper: Path,
        fake_bin: Path,
        registry: Path,
        marker_state: Path,
        signal_log: Path,
        run_id: str,
        valid_markers: int,
        helper_command: str,
    ) -> subprocess.CompletedProcess[str]:
        # Source the production script unchanged so this function intercepts the shell builtin
        # safely; the shims control every process-inspection result deterministically.
        harness = """
fake_bin=$1
registry=$2
marker_state=$3
signal_log=$4
helper=$5
run_id=$6
valid_markers=$7
helper_command=$8
PATH="$fake_bin:/usr/bin:/bin"
GP_ROOT_HELPER_RUN_DIR="$registry"
FAKE_MARKER_STATE="$marker_state"
FAKE_SIGNAL_LOG="$signal_log"
FAKE_VALID_MARKERS="$valid_markers"
FAKE_PROCESS_ID=$$
printf 'helper-v1 %s %s 101\n' "$FAKE_PROCESS_ID" "$FAKE_PROCESS_ID" > "$registry/$run_id"
export PATH GP_ROOT_HELPER_RUN_DIR FAKE_MARKER_STATE FAKE_SIGNAL_LOG FAKE_VALID_MARKERS FAKE_PROCESS_ID
kill() { printf '%s\\n' "$*" >> "$FAKE_SIGNAL_LOG"; }
lock_dir="$registry/.$run_id.lock"
mkdir "$lock_dir"
printf 'helper-ready-v2 %s %s 101\n' "$FAKE_PROCESS_ID" "$FAKE_PROCESS_ID" > "$lock_dir/supervisor-ready"
printf 'helper-go-v1 %s\n' "$FAKE_PROCESS_ID" > "$lock_dir/supervisor-go"
printf 'helper-status-v1 7\n' > "$lock_dir/target-status"
: > "$lock_dir/signal-gate"
case "$helper_command" in
  signal-run) set -- signal-run "$run_id" TERM ;;
  recover-runs) set -- recover-runs ;;
esac
. "$helper"
"""
        return subprocess.run(
            [
                shell,
                "-c",
                harness,
                "root-helper-shim",
                _posix_shell_path(fake_bin),
                _posix_shell_path(registry),
                _posix_shell_path(marker_state),
                _posix_shell_path(signal_log),
                _posix_shell_path(helper),
                run_id,
                str(valid_markers),
                helper_command,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_run_owned_cleanup_ignores_zombies_but_preserves_live_groups_with_portable_shell_shims(self) -> None:
        """Exercise the real helper body using deterministic ps states rather than real zombies.

        The shims model only host process inspection and group delivery.  The production
        supervisor, target-status protocol, record writing, and cleanup paths run unchanged.
        """
        if sys.platform != "linux":
            self.skipTest("requires Linux procfs and process-group semantics; Git Bash is not a compatible substitute")
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        for after_kill_state, expected_code, artifacts_removed in (
            ("Z", 7, True),
            ("Z+", 7, True),
            ("S", None, False),
        ):
            with self.subTest(after_kill_state=after_kill_state):
                with tempfile.TemporaryDirectory() as raw:
                    root = Path(raw)
                    registry = root / "runs"
                    registry.mkdir()
                    (root / "gates").mkdir()
                    fake_bin = root / "fake-bin"
                    fake_bin.mkdir()
                    child_pid_path = root / "child.pid"
                    signal_log = root / "signals.log"
                    supervisor_pid_path = root / "supervisor.pid"
                    sleep_pid_path = root / "supervisor-sleep.pid"
                    phase_path = root / "phase"
                    run_id = "portable-target-status"
                    lock_dir = registry / f".{run_id}.lock"
                    status_file = lock_dir / "target-status"
                    target = root / "blockcheck2.sh"
                    target.write_text(
                        "#!/bin/sh\n"
                        "(trap '' TERM; exec tail -f /dev/null >/dev/null 2>&1) &\n"
                        "printf '%s\\n' \"$!\" > \"$1\"\n"
                        "exit 7\n",
                        encoding="utf-8",
                    )
                    target.chmod(0o700)
                    self._write_run_owned_lifecycle_shims(fake_bin)
                    helper_copy = root / "gp-root-helper-with-test-gate.sh"
                    helper_copy.write_text(
                        _root_helper_test_source(helper).replace(
                            "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                            f"DISCOVERY_GATE_DIR='{_posix_shell_path(root / 'gates')}'",
                        ).replace(
                            "\nrequire_root\n",
                            '\nsignal_known_process_group() { kill "-$1" -- "$2"; }\n\nrequire_root\n',
                        ),
                        encoding="utf-8",
                    )

                    completed = self._run_owned_with_lifecycle_shims(
                        shell=shell,
                        helper=helper_copy,
                        fake_bin=fake_bin,
                        root=root,
                        registry=registry,
                        target=target,
                        child_pid_path=child_pid_path,
                        signal_log=signal_log,
                        supervisor_pid_path=supervisor_pid_path,
                        sleep_pid_path=sleep_pid_path,
                        phase_path=phase_path,
                        status_file=status_file,
                        run_id=run_id,
                        after_kill_state=after_kill_state,
                    )

                    if expected_code is None:
                        self.assertNotEqual(completed.returncode, 0, completed.stderr)
                        self.assertIn("managed process group could not be safely cleaned up (status 2)", completed.stderr)
                    else:
                        self.assertEqual(completed.returncode, expected_code, completed.stderr)
                    self.assertEqual(signal_log.read_text(encoding="utf-8").splitlines(), ["TERM", "KILL"])
                    self.assertEqual((root / "term-observed-status").read_text(encoding="utf-8"), "live\n")
                    self.assertEqual((root / "child-killed").read_text(encoding="utf-8"), "yes\n")
                    self.assertEqual((root / "child-reaped").read_text(encoding="utf-8"), "yes\n")
                    self.assertFalse((root / "emergency-cleanup-used").exists())
                    self.assertEqual((registry / run_id).exists(), not artifacts_removed)
                    self.assertEqual(status_file.exists(), not artifacts_removed)
                    self.assertEqual(lock_dir.exists(), not artifacts_removed)

    def test_owned_multidomain_setup_failure_removes_only_its_generated_directory_portably(self) -> None:
        """The wrapper must clean its private directory even before ownership starts."""
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp_root = root / "tmp"
            temp_root.mkdir()
            keep = temp_root / "keep"
            keep.mkdir()
            (keep / "sentinel").write_text("keep\n", encoding="utf-8")
            source = root / "blockcheck2.sh"
            source.write_text("#!/bin/sh\n# fsleep_setup marker deliberately absent\n", encoding="utf-8")
            source.chmod(0o700)

            completed = _run_owned_multidomain_library(
                shell=shell,
                helper=helper,
                source=source,
                temp_root=temp_root,
                run_id="owned-md-setup-failure",
                lifecycle="return 0",
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("main marker not found", completed.stderr)
            self.assertEqual(sorted(path.name for path in temp_root.iterdir()), ["keep"])
            self.assertEqual((keep / "sentinel").read_text(encoding="utf-8"), "keep\n")

    def test_owned_multidomain_early_term_cleans_private_window_before_runner_handoff_portably(self) -> None:
        """TERM after mktemp must remove only this wrapper's directory before handoff."""
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp_root = root / "tmp"
            temp_root.mkdir()
            keep = temp_root / "keep"
            keep.mkdir()
            (keep / "sentinel").write_text("keep\n", encoding="utf-8")
            source = root / "blockcheck2.sh"
            source.write_text("#!/bin/sh\n", encoding="utf-8")
            source.chmod(0o700)
            library = root / "root-helper-library.sh"
            library.write_text(_root_helper_test_source(helper).split("\nrequire_root\n", 1)[0] + "\n", encoding="utf-8")
            runner_path = root / "runner-path"
            handoff = root / "handoff"
            harness = '''\
. "$1"
write_multidomain_runner() {
  printf '%s\\n' "$2" > "$GP_TEST_RUNNER_PATH"
  # Invoke the production TERM handler directly: MSYS defers a self-sent signal
  # until this function returns, which would incorrectly cross the handoff boundary.
  abort_multidomain_owned_run 143
}
run_owned_process() {
  : > "$GP_TEST_HANDOFF"
  return 0
}
run_owned_multidomain_target "$2" "$3"
'''
            env = {
                **os.environ,
                "PATH": "/usr/bin:/bin",
                "TMPDIR": _posix_shell_path(temp_root),
                "ZAPRET_DIR": _posix_shell_path(root),
                "GP_TEST_RUNNER_PATH": _posix_shell_path(runner_path),
                "GP_TEST_HANDOFF": _posix_shell_path(handoff),
            }
            completed = subprocess.run(
                [
                    shell,
                    "-c",
                    harness,
                    "owned-multidomain-early-term",
                    _posix_shell_path(library),
                    "owned-md-early-term",
                    _posix_shell_path(source),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertIn(completed.returncode, (143, 15 * 256), completed.stderr)
            runner = _native_shell_path(runner_path.read_text(encoding="utf-8").strip())
            self.assertFalse(runner.exists())
            self.assertFalse(runner.parent.exists())
            self.assertFalse(handoff.exists(), "TERM before runner generation must not hand off to the owned supervisor")
            self.assertEqual(sorted(path.name for path in temp_root.iterdir()), ["keep"])
            self.assertEqual((keep / "sentinel").read_text(encoding="utf-8"), "keep\n")

    def test_owned_multidomain_normal_lifecycle_cleans_generated_runner_once_without_trap_replacement(self) -> None:
        """The wrapper owns its temp-dir trap; the supervisor runs in a child trap scope."""
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp_root = root / "tmp"
            temp_root.mkdir()
            keep = temp_root / "keep"
            keep.mkdir()
            (keep / "sentinel").write_text("keep\n", encoding="utf-8")
            runner_path = root / "runner-path"
            hook_log = root / "hook.log"
            source = root / "blockcheck2.sh"
            source.write_text(_minimal_multidomain_blockcheck_source(), encoding="utf-8")
            source.chmod(0o700)

            completed = _run_owned_multidomain_library(
                shell=shell,
                helper=helper,
                source=source,
                temp_root=temp_root,
                run_id="owned-md-hook-once",
                lifecycle=(
                    'printf "%s\\n" "$runner" > "$GP_TEST_RUNNER_PATH"\n'
                    '[ -x "$runner" ] || exit 91\n'
                    'return 23'
                ),
                extra_env={
                    "GP_TEST_RUNNER_PATH": _posix_shell_path(runner_path),
                    "GP_TEST_RM_LOG": _posix_shell_path(hook_log),
                },
            )

            self.assertEqual(completed.returncode, 23, completed.stderr)
            runner_text = runner_path.read_text(encoding="utf-8").strip()
            runner = _native_shell_path(runner_text)
            self.assertEqual(runner.name, "gp-multidomain-blockcheck.sh")
            runner_dir = runner_text.rsplit("/", 1)[0]
            cleanup_calls = [line for line in hook_log.read_text(encoding="utf-8").splitlines() if line == f"-rf -- {runner_dir}"]
            self.assertEqual(cleanup_calls, [f"-rf -- {runner_dir}"])
            self.assertFalse(runner.exists())
            self.assertFalse(runner.parent.exists())
            self.assertEqual(sorted(path.name for path in temp_root.iterdir()), ["keep"])
            self.assertEqual((keep / "sentinel").read_text(encoding="utf-8"), "keep\n")

        helper_text = (Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh").read_text(encoding="utf-8")
        owned_wrapper = helper_text.split("run_owned_multidomain_target() (", 1)[1].split("\n)\n\nrequire_root", 1)[0]
        self.assertIn('( run_owned_process "$run_id" "$runner" "$@" )', owned_wrapper)
        self.assertNotIn("--cleanup-dir", owned_wrapper)
        self.assertIn("trap cleanup_runner EXIT", owned_wrapper)

    def test_root_owned_multidomain_normal_completion_cleans_only_its_runner_directory(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp_root = root / "tmp"
            temp_root.mkdir()
            keep = temp_root / "keep"
            keep.mkdir()
            (keep / "sentinel").write_text("keep\n", encoding="utf-8")
            runner_path = root / "runner-path"
            source = root / "blockcheck2.sh"
            source.write_text(_minimal_multidomain_blockcheck_source(), encoding="utf-8")
            source.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(root),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "TMPDIR": str(temp_root),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
                "GP_TEST_RUNNER_PATH": str(runner_path),
            }

            completed = subprocess.run(
                ["sh", str(helper), "run-multidomain-owned", "owned-md-normal", str(source)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            runner = Path(runner_path.read_text(encoding="utf-8").strip())
            self.assertFalse(runner.exists())
            self.assertFalse(runner.parent.exists())
            self.assertFalse((registry / "owned-md-normal").exists())
            self.assertFalse((registry / ".owned-md-normal.lock").exists())
            self.assertEqual(sorted(path.name for path in temp_root.iterdir()), ["keep"])
            self.assertEqual((keep / "sentinel").read_text(encoding="utf-8"), "keep\n")

    def test_root_owned_multidomain_duplicate_term_is_safe_through_terminal_teardown(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            temp_root = root / "tmp"
            temp_root.mkdir()
            keep = temp_root / "keep"
            keep.mkdir()
            (keep / "sentinel").write_text("keep\n", encoding="utf-8")
            runner_path = root / "runner-path"
            started = root / "runner-started"
            release = root / "release"
            os.mkfifo(release)
            source = root / "blockcheck2.sh"
            source.write_text(_minimal_multidomain_blockcheck_source(wait_for_release=True), encoding="utf-8")
            source.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(root),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "TMPDIR": str(temp_root),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
                "GP_TEST_RUNNER_PATH": str(runner_path),
                "GP_TEST_STARTED": str(started),
                "GP_TEST_RELEASE": str(release),
            }
            run_id = "owned-md-external-term"
            managed = subprocess.Popen(["sh", str(helper), "run-multidomain-owned", run_id, str(source)], env=env)
            try:
                _wait_for_path(started)
                _wait_for_path(runner_path)
                _wait_for_path(registry / run_id)
                runner = Path(runner_path.read_text(encoding="utf-8").strip())
                self.assertTrue(runner.exists())

                stopped = subprocess.run(
                    ["sh", str(helper), "signal-run", run_id, "TERM"], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                # signal-run may report the target group as stopped before the
                # wrapper observes target-status. Its original launcher must
                # still reap promptly. Its validated lifecycle evidence stays
                # intact in the terminal directory until the direct caller has
                # reaped that launcher and explicitly acknowledges it.
                self.assertNotEqual(managed.wait(timeout=8), 0)
                self.assertFalse(runner.exists())
                self.assertFalse(runner.parent.exists())
                self.assertFalse((registry / run_id).exists())
                self.assertFalse((registry / f".{run_id}.lock").exists())
                terminal_dir = registry / f".{run_id}.terminal"
                self.assertTrue(terminal_dir.is_dir())

                duplicate = subprocess.run(
                    ["sh", str(helper), "signal-run", run_id, "TERM"], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                self.assertNotIn("unsafe", duplicate.stderr)
                self.assertTrue(terminal_dir.is_dir())

                acknowledged = subprocess.run(
                    ["sh", str(helper), "ack-run-terminal", run_id], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)
                self.assertFalse(terminal_dir.exists())
                self.assertFalse(_any_process_carries_argument(run_id))
                self.assertEqual(sorted(path.name for path in temp_root.iterdir()), ["keep"])
                self.assertEqual((keep / "sentinel").read_text(encoding="utf-8"), "keep\n")
            finally:
                if managed.poll() is None:
                    subprocess.run(["sh", str(helper), "signal-run", run_id, "KILL"], env=env, check=False)
                    managed.wait(timeout=5)

    def _write_run_owned_lifecycle_shims(self, fake_bin: Path) -> None:
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
        (fake_bin / "install").write_text(
            "#!/bin/sh\nfor destination do :; done\nmkdir -p \"$destination\"\n", encoding="utf-8"
        )
        (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "stat").write_text(
            "#!/bin/sh\ncase \"$*\" in *discovery-update.lock*) printf '0:0:600\\n' ;; *.lock) printf '0:0:700\\n' ;; *signal-gate*|*signal-delivery*|*/runs/*) printf '0:0:600\\n' ;; */runs) printf '0:0:750\\n' ;; *) printf '0:0:700\\n' ;; esac\n",
            encoding="utf-8",
        )
        (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "setsid").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" > \"$FAKE_SUPERVISOR_PID_PATH\"\nexec \"$@\"\n", encoding="utf-8"
        )
        (fake_bin / "sleep").write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = 2147483647 ]; then\n"
            "  printf '%s\\n' \"$$\" > \"$FAKE_SLEEP_PID_PATH\"\n"
            "  exec tail -f /dev/null >/dev/null 2>&1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fake_bin / "ps").write_text(
            "#!/bin/sh\n"
            "while [ ! -s \"$FAKE_SUPERVISOR_PID_PATH\" ]; do :; done\n"
            "supervisor=$(cat \"$FAKE_SUPERVISOR_PID_PATH\")\n"
            "case \"$*\" in\n"
            "  *'-e -o pid= -o pgid= -o sid='*) [ \"$(cat \"$FAKE_PHASE_PATH\")\" = gone ] || printf '%s %s %s\\n' \"$supervisor\" \"$supervisor\" \"$supervisor\" ;;\n"
            "  *'-e -o pgid= -o sid= -o stat='*) printf '%s %s %s\\n' \"$supervisor\" \"$supervisor\" \"$(cat \"$FAKE_PHASE_PATH\")\" ;;\n"
            "  *'-o pgid='*) printf '%s\\n' \"$supervisor\" ;;\n"
            "  *'-o sid='*) printf '%s\\n' \"$supervisor\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        for shim in fake_bin.iterdir():
            shim.chmod(0o700)

    def _run_owned_with_lifecycle_shims(
        self,
        *,
        shell: str,
        helper: Path,
        fake_bin: Path,
        root: Path,
        registry: Path,
        target: Path,
        child_pid_path: Path,
        signal_log: Path,
        supervisor_pid_path: Path,
        sleep_pid_path: Path,
        phase_path: Path,
        status_file: Path,
        run_id: str,
        after_kill_state: str,
    ) -> subprocess.CompletedProcess[str]:
        phase_path.write_text("S\n", encoding="utf-8")
        harness = """
PATH="$1:/usr/bin:/bin"
ZAPRET_DIR="$2"
GP_ROOT_HELPER_RUN_DIR="$3"
FAKE_CHILD_PID_PATH="$4"
FAKE_SIGNAL_LOG="$5"
FAKE_SUPERVISOR_PID_PATH="$6"
FAKE_SLEEP_PID_PATH="$7"
FAKE_PHASE_PATH="$8"
FAKE_STATUS_FILE="$9"
FAKE_TERM_OBSERVED_STATUS="${10}"
FAKE_AFTER_KILL_STATE="${11}"
FAKE_CHILD_REAPED_PATH="$(dirname "$FAKE_CHILD_PID_PATH")/child-reaped"
FAKE_EMERGENCY_CLEANUP_PATH="$(dirname "$FAKE_CHILD_PID_PATH")/emergency-cleanup-used"
export PATH ZAPRET_DIR GP_ROOT_HELPER_RUN_DIR FAKE_CHILD_PID_PATH FAKE_SIGNAL_LOG FAKE_SUPERVISOR_PID_PATH FAKE_SLEEP_PID_PATH FAKE_PHASE_PATH FAKE_STATUS_FILE FAKE_TERM_OBSERVED_STATUS FAKE_AFTER_KILL_STATE FAKE_CHILD_REAPED_PATH FAKE_EMERGENCY_CLEANUP_PATH
fixture_member_is_reaped() {
  fixture_pid="$1"
  if [ -r "/proc/$fixture_pid/stat" ]; then
    fixture_stat="$(command cat "/proc/$fixture_pid/stat" 2>/dev/null)" || return 0
    case "$fixture_stat" in
      *') Z '*|*') X '*) return 0 ;;
    esac
    return 1
  fi
  command kill -0 "$fixture_pid" 2>/dev/null && return 1
  return 0
}
wait_for_fixture_member() {
  fixture_pid="$1"
  fixture_waited=0
  # The supervisor is a child of this harness; the sleep and target children
  # can be reparented after KILL, so verify their disappearance after wait too.
  wait "$fixture_pid" 2>/dev/null || true
  while ! fixture_member_is_reaped "$fixture_pid"; do
    [ "$fixture_waited" -lt 200 ] || return 1
    /usr/bin/sleep 0.01 2>/dev/null || true
    fixture_waited=$((fixture_waited + 1))
  done
}
kill_fixture_member() {
  fixture_path="$1"
  [ -s "$fixture_path" ] || return 0
  fixture_pid="$(command cat "$fixture_path")" || return 1
  if ! fixture_member_is_reaped "$fixture_pid"; then
    command kill -KILL "$fixture_pid" 2>/dev/null || return 1
  fi
  wait_for_fixture_member "$fixture_pid"
}
emergency_cleanup_fixture_members() {
  for fixture_path in "$FAKE_SUPERVISOR_PID_PATH" "$FAKE_SLEEP_PID_PATH" "$FAKE_CHILD_PID_PATH"; do
    [ -s "$fixture_path" ] || continue
    fixture_pid="$(command cat "$fixture_path")" || continue
    if ! fixture_member_is_reaped "$fixture_pid"; then
      printf 'yes\n' > "$FAKE_EMERGENCY_CLEANUP_PATH"
      command kill -KILL "$fixture_pid" 2>/dev/null || true
      wait_for_fixture_member "$fixture_pid" || true
    fi
  done
}
trap emergency_cleanup_fixture_members EXIT
kill() {
  case "$*" in
    *-TERM*)
      printf 'TERM\n' >> "$FAKE_SIGNAL_LOG"
      supervisor="$(command cat "$FAKE_SUPERVISOR_PID_PATH")"
      if [ -f "$FAKE_STATUS_FILE" ] && command kill -0 "$supervisor" 2>/dev/null; then
        printf 'live\n' > "$FAKE_TERM_OBSERVED_STATUS"
      fi
      ;;
    *-KILL*)
      printf 'KILL\n' >> "$FAKE_SIGNAL_LOG"
      kill_fixture_member "$FAKE_SUPERVISOR_PID_PATH" || return 1
      kill_fixture_member "$FAKE_SLEEP_PID_PATH" || return 1
      kill_fixture_member "$FAKE_CHILD_PID_PATH" || return 1
      printf '%s\n' "$FAKE_AFTER_KILL_STATE" > "$FAKE_PHASE_PATH"
      printf 'yes\n' > "$(dirname "$FAKE_CHILD_PID_PATH")/child-killed"
      printf 'yes\n' > "$FAKE_CHILD_REAPED_PATH"
      ;;
    *) command kill "$@" ;;
  esac
}
helper="${12}"
set -- run-owned "${13}" "${14}" "$4"
. "$helper"
"""
        return subprocess.run(
            [
                shell,
                "-c",
                harness,
                "root-helper-run-owned-shim",
                _posix_shell_path(fake_bin),
                _posix_shell_path(root),
                _posix_shell_path(registry),
                _posix_shell_path(child_pid_path),
                _posix_shell_path(signal_log),
                _posix_shell_path(supervisor_pid_path),
                _posix_shell_path(sleep_pid_path),
                _posix_shell_path(phase_path),
                _posix_shell_path(status_file),
                _posix_shell_path(root / "term-observed-status"),
                after_kill_state,
                _posix_shell_path(helper),
                run_id,
                _posix_shell_path(target),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=8,
        )

    def test_run_owned_handshake_refuses_to_start_target_before_attestation(self) -> None:
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        text = helper.read_text(encoding="utf-8")
        run_owned = text.split("run_owned_process() {", 1)[1].split("run_owned_target() {", 1)[0]

        self.assertIn('helper-ready-v1 %s', run_owned)
        self.assertIn('write_owned_run_attestation "$ready_file" "$pid" "$pgid" "$marker"', run_owned)
        self.assertIn('helper-go-v1 $$', run_owned)
        self.assertLess(run_owned.index('wait_for_owned_run_ready'), run_owned.index('write_owned_run_record'))
        self.assertLess(run_owned.index('write_owned_run_attestation'), run_owned.index('write_owned_run_record'))
        self.assertLess(run_owned.index('write_owned_run_record'), run_owned.index('write_owned_run_go'))
        supervisor_body = run_owned.split("setsid /bin/sh -c '", 1)[1].split("' gp-owned-supervisor", 1)[0]
        self.assertLess(supervisor_body.index('[ "$go_contents" = "helper-go-v1 $$" ]'), supervisor_body.index('( trap - HUP INT TERM; exec "$@" <&7 7<&- ) &'))
        self.assertIn('stop_unattested_supervisor || return 1', run_owned)
        self.assertIn('kill -TERM "$pid"', run_owned)
        self.assertNotIn('kill -TERM -- "-$pid"', run_owned)
        self.assertIn('terminate_known_process_group "$pid" "$pgid" "$marker" TERM', run_owned)
        cleanup_locked = run_owned.split("cleanup_owned_run_locked() {", 1)[1].split("cleanup_owned_run() {", 1)[0]
        cleanup_wrapper = run_owned.split("cleanup_owned_run() {", 1)[1].split("cleanup_owned_lifecycle() {", 1)[0]
        self.assertLess(
            cleanup_locked.index('stop_unattested_supervisor || return 1'),
            cleanup_locked.index('remove_unattested_run_lock'),
        )
        self.assertIn('with_run_lifecycle_gate "$lifecycle_gate" cleanup_owned_run_locked', cleanup_wrapper)
        self.assertIn('lifecycle_gate="$lock_dir/signal-gate"', run_owned)
        self.assertIn('signal_file="$lock_dir/signal-delivery"', run_owned)
        self.assertIn('mv -- "$lock_dir" "$terminal_dir"', run_owned)
        self.assertIn('if ! write_owned_run_record', run_owned)
        self.assertIn('if ! write_owned_run_go', run_owned)
        self.assertIn("trap 'abort_owned_run 143' TERM", run_owned)

    def test_root_helper_kills_snapshot_child_after_term_when_leader_is_gone(self) -> None:
        if sys.platform != "linux":
            self.skipTest("requires Linux procfs and process-group semantics; Git Bash is not a compatible substitute")
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter to execute the root-helper")
        if subprocess.run([shell, "-c", "[ -r /proc/$$/stat ]"], check=False).returncode != 0:
            self.skipTest("requires the /proc start-marker interface used by the root-helper")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            (root / "gates").mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            signal_log = root / "signals.log"
            self._write_root_helper_snapshot_shims(fake_bin)

            completed = self._run_root_helper_with_snapshot_shims(
                shell=shell,
                helper=helper,
                fake_bin=fake_bin,
                registry=registry,
                signal_log=signal_log,
                run_id="snapshot-child-leader-gone",
                leader_after_term_marker=None,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            actual_signals = signal_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(actual_signals), 2, actual_signals)
            self.assertRegex(actual_signals[0], r"^-TERM -- -[1-9][0-9]*$")
            self.assertRegex(actual_signals[1], r"^-KILL -- -[1-9][0-9]*$")
            self.assertFalse((registry / "snapshot-child-leader-gone").exists())

    def test_root_helper_refuses_kill_when_leader_marker_changes_after_term(self) -> None:
        if sys.platform != "linux":
            self.skipTest("requires Linux procfs and process-group semantics; Git Bash is not a compatible substitute")
        shell = _posix_shell()
        if shell is None:
            self.skipTest("requires a POSIX sh interpreter to execute the root-helper")
        if subprocess.run([shell, "-c", "[ -r /proc/$$/stat ]"], check=False).returncode != 0:
            self.skipTest("requires the /proc start-marker interface used by the root-helper")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            registry.mkdir()
            (root / "gates").mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            signal_log = root / "signals.log"
            self._write_root_helper_snapshot_shims(fake_bin)

            completed = self._run_root_helper_with_snapshot_shims(
                shell=shell,
                helper=helper,
                fake_bin=fake_bin,
                registry=registry,
                signal_log=signal_log,
                run_id="snapshot-child-leader-reused",
                leader_after_term_marker="202",
            )

            self.assertEqual(completed.returncode, 126, completed.stderr)
            self.assertIn("stale or invalid", completed.stderr)
            actual_signals = signal_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(actual_signals), 1, actual_signals)
            self.assertRegex(actual_signals[0], r"^-TERM -- -[1-9][0-9]*$")
            self.assertTrue((registry / "snapshot-child-leader-reused").exists())

    def _write_root_helper_snapshot_shims(self, fake_bin: Path) -> None:
        (fake_bin / "id").write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
        (fake_bin / "install").write_text(
            "#!/bin/sh\nfor destination do :; done\nmkdir -p \"$destination\"\n", encoding="utf-8"
        )
        (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "chmod").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "stat").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *discovery-update.lock*) printf '0:0:600\\n' ;;\n"
            "  */gates) printf '0:0:700\\n' ;;\n"
            "  *.lock) printf '0:0:700\\n' ;;\n"
            "  *) printf '0:0:600\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "ps").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'-e -o pid= -o pgid= -o sid='*) printf '%s %s %s\\n%s %s %s\\n' \"$FAKE_LEADER_PID\" \"$FAKE_LEADER_PID\" \"$FAKE_LEADER_PID\" \"$FAKE_CHILD_PID\" \"$FAKE_LEADER_PID\" \"$FAKE_LEADER_PID\" ;;\n"
            "  *'-e -o pgid= -o sid= -o stat='*)\n"
            "    if [ \"$(cat \"$FAKE_PHASE\")\" = killed ]; then\n"
            "      printf '%s %s Z\\n' \"$FAKE_LEADER_PID\" \"$FAKE_LEADER_PID\"\n"
            "    else\n"
            "      printf '%s %s S\\n' \"$FAKE_LEADER_PID\" \"$FAKE_LEADER_PID\"\n"
            "    fi\n"
            "    ;;\n"
            "  *'-o pgid='*) printf '%s\\n' \"$FAKE_LEADER_PID\" ;;\n"
            "  *'-o sid='*) printf '%s\\n' \"$FAKE_LEADER_PID\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        (fake_bin / "awk").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'$2 == pgid'*) printf '%s\\n%s\\n' \"$FAKE_LEADER_PID\" \"$FAKE_CHILD_PID\" ;;\n"
            "  *'$1 == pgid'*) [ \"$(cat \"$FAKE_PHASE\")\" = killed ] && exit 1; exit 0 ;;\n"
            "  *'print substr(stat_tail, 1, 1)'*\"/proc/$FAKE_LEADER_PID/stat\"*)\n"
            "    if [ \"$(cat \"$FAKE_PHASE\")\" = killed ]; then printf 'Z\\n'; else printf 'S\\n'; fi\n"
            "    ;;\n"
            "  *'print substr(stat_tail, 1, 1)'*\"/proc/$FAKE_CHILD_PID/stat\"*) printf 'S\\n' ;;\n"
            "  *\"/proc/$FAKE_LEADER_PID/stat\"*)\n"
            "    if [ \"$(cat \"$FAKE_PHASE\")\" = after-term ]; then\n"
            "      [ -n \"$FAKE_LEADER_AFTER_TERM_MARKER\" ] && printf '%s\\n' \"$FAKE_LEADER_AFTER_TERM_MARKER\"\n"
            "    else\n"
            "      printf '101\\n'\n"
            "    fi\n"
            "    ;;\n"
            "  *\"/proc/$FAKE_CHILD_PID/stat\"*) printf '102\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        for shim in fake_bin.iterdir():
            shim.chmod(0o700)

    def _run_root_helper_with_snapshot_shims(
        self,
        *,
        shell: str,
        helper: Path,
        fake_bin: Path,
        registry: Path,
        signal_log: Path,
        run_id: str,
        leader_after_term_marker: str | None,
    ) -> subprocess.CompletedProcess[str]:
        helper_copy = registry.parent / "gp-root-helper-snapshot-test.sh"
        helper_copy.write_text(
            _root_helper_test_source(helper).replace(
                "DISCOVERY_GATE_DIR='/run/gp-control-plane/gates'",
                f"DISCOVERY_GATE_DIR='{_posix_shell_path(registry.parent / 'gates')}'",
            ).replace(
                "\nrequire_root\n",
                '\nsignal_known_process_group() { kill "-$1" -- "-$2"; }\n\nrequire_root\n',
            ),
            encoding="utf-8",
        )
        harness = """
fake_bin=$1
registry=$2
signal_log=$3
helper=$4
run_id=$5
leader_after_term_marker=$6
python_executable=$7
"$python_executable" -c 'import time; time.sleep(300)' &
FAKE_LEADER_PID=$!
"$python_executable" -c 'import time; time.sleep(300)' &
FAKE_CHILD_PID=$!
PATH="$fake_bin:/usr/bin:/bin"
GP_ROOT_HELPER_RUN_DIR="$registry"
FAKE_SIGNAL_LOG="$signal_log"
FAKE_PHASE="$registry/phase"
FAKE_LEADER_AFTER_TERM_MARKER="$leader_after_term_marker"
printf 'before-term\n' > "$FAKE_PHASE"
printf 'helper-v1 %s %s 101\n' "$FAKE_LEADER_PID" "$FAKE_LEADER_PID" > "$registry/$run_id"
lock_dir="$registry/.$run_id.lock"
mkdir "$lock_dir"
printf 'helper-ready-v2 %s %s 101\n' "$FAKE_LEADER_PID" "$FAKE_LEADER_PID" > "$lock_dir/supervisor-ready"
printf 'helper-go-v1 %s\n' "$FAKE_LEADER_PID" > "$lock_dir/supervisor-go"
: > "$lock_dir/signal-gate"
export PATH GP_ROOT_HELPER_RUN_DIR FAKE_SIGNAL_LOG FAKE_LEADER_PID FAKE_CHILD_PID FAKE_PHASE FAKE_LEADER_AFTER_TERM_MARKER
cleanup() {
  for process_pid in "$FAKE_LEADER_PID" "$FAKE_CHILD_PID"; do
    command kill -KILL "$process_pid" 2>/dev/null || true
    wait "$process_pid" 2>/dev/null || true
  done
}
trap cleanup EXIT
kill() {
  printf '%s\n' "$*" >> "$FAKE_SIGNAL_LOG"
  case "$*" in
    *-TERM*)
      printf 'after-term\n' > "$FAKE_PHASE"
      if [ -z "$FAKE_LEADER_AFTER_TERM_MARKER" ]; then
        command kill -KILL "$FAKE_LEADER_PID" 2>/dev/null || true
        wait "$FAKE_LEADER_PID" 2>/dev/null || true
      fi
      ;;
    *-KILL*) printf 'killed\n' > "$FAKE_PHASE" ;;
  esac
}
set -- signal-run "$run_id" TERM
. "$helper"
"""
        return subprocess.run(
            [
                shell,
                "-c",
                harness,
                "root-helper-snapshot-shim",
                _posix_shell_path(fake_bin),
                _posix_shell_path(registry),
                _posix_shell_path(signal_log),
                _posix_shell_path(helper_copy),
                run_id,
                leader_after_term_marker or "",
                _posix_shell_path(Path(sys.executable)),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_root_run_owned_reaps_term_ignoring_child_and_returns_target_code(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child_pid_path = root / "child.pid"
            target = root / "blockcheck2.sh"
            target.write_text(
                "#!/bin/sh\n"
                "(trap '' TERM; while :; do sleep 30 & wait $!; done) &\n"
                "child=$!\n"
                "printf '%s\\n' \"$child\" > \"$1\"\n"
                "exit 7\n",
                encoding="utf-8",
            )
            target.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(root),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
            }
            run_id = "reap-background-child"
            managed = subprocess.Popen(["sh", str(helper), "run-owned", run_id, str(target), str(child_pid_path)], env=env)
            record = registry / run_id
            try:
                _wait_for_path(child_pid_path)
                _wait_for_path(record)
                child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())

                self.assertEqual(managed.wait(timeout=5), 7)
                self.assertFalse(record.exists())
                _wait_for_pid_to_exit(child_pid)
            finally:
                if managed.poll() is None:
                    subprocess.run(["sh", str(helper), "signal-run", run_id, "KILL"], env=env, check=False)
                    managed.wait(timeout=5)

    def test_root_signal_after_go_reaps_term_ignoring_target_and_child(self) -> None:
        dash = Path("/bin/dash")
        if (
            sys.platform != "linux"
            or not hasattr(os, "geteuid")
            or os.geteuid() != 0
            or not shutil.which("setsid")
            or not dash.is_file()
        ):
            self.skipTest("requires a root Linux test environment with setsid and /bin/dash")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = root / "runs"
            started = root / "target-started"
            child_pid_path = root / "child.pid"
            target = root / "blockcheck2.sh"
            target.write_text(
                "#!/bin/sh\n"
                "printf started > \"$1\"\n"
                "(trap '' TERM; while :; do sleep 30 & wait $!; done) &\n"
                "child=$!\n"
                "printf '%s\\n' \"$child\" > \"$2\"\n"
                "trap '' TERM\n"
                "wait \"$child\"\n",
                encoding="utf-8",
            )
            target.chmod(0o700)
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(root),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
            }
            run_id = "signal-after-go-term-ignored"
            managed = subprocess.Popen(
                [str(dash), str(helper), "run-owned", run_id, str(target), str(started), str(child_pid_path)], env=env
            )
            record = registry / run_id
            lock_dir = registry / f".{run_id}.lock"
            try:
                _wait_for_path(started)
                _wait_for_path(child_pid_path)
                _wait_for_path(record)
                version, pid, pgid, marker = record.read_text(encoding="utf-8").split()
                self.assertEqual(version, "helper-v1")
                self.assertEqual(pgid, pid)
                self.assertEqual(marker, Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(") ")[2].split()[19])
                child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
                unrelated_record = registry / "unrelated"
                unrelated_lock = registry / ".unrelated.lock"
                unrelated_record.write_text("leave this record alone\n", encoding="utf-8")
                unrelated_lock.mkdir()

                stopped = subprocess.run(
                    [str(dash), str(helper), "signal-run", run_id, "TERM"], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertEqual(managed.wait(timeout=8), 126)
                self.assertFalse(record.exists())
                self.assertFalse(lock_dir.exists())
                self.assertEqual(unrelated_record.read_text(encoding="utf-8"), "leave this record alone\n")
                self.assertTrue(unrelated_lock.is_dir())
                _wait_for_pid_to_exit(child_pid)
            finally:
                if managed.poll() is None:
                    subprocess.run([str(dash), str(helper), "signal-run", run_id, "KILL"], env=env, check=False)
                    managed.wait(timeout=5)

    def test_root_helper_creates_the_only_signalable_record_and_rejects_direct_registration(self) -> None:
        if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "blockcheck2.sh"
            started = root / "target-started"
            target.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nprintf started > \"$1\"\nsleep 30\n", encoding="utf-8")
            target.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(root)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(root),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
            }
            run_id = "helper-owned-run"
            managed = subprocess.Popen(["sh", str(helper), "run-owned", run_id, str(target), str(started)], env=env)
            try:
                record = registry / run_id
                _wait_for_path(record)
                self.assertEqual(record.read_text(encoding="utf-8").split()[0], "helper-v1")
                ready = registry / f".{run_id}.lock" / "supervisor-ready"
                _wait_for_path(ready)
                self.assertEqual(ready.read_text(encoding="utf-8").split()[0], "helper-ready-v2")
                # The record/attestation deliberately precede GO. Exercise
                # TERM only after the target has entered its own lifecycle.
                _wait_for_path(started)
                attestation = ready.read_text(encoding="utf-8")

                rejected = subprocess.run(
                    ["sh", str(helper), "register-run", "foreign-pid", str(os.getpid()), str(os.getpgrp()), "1"],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(rejected.returncode, 126)
                self.assertTrue(record.exists())

                stopped = subprocess.run(
                    ["sh", str(helper), "signal-run", run_id, "TERM"], env=env, text=True, capture_output=True, check=False
                )
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                # Registered TERM stops the attested supervisor too. Its
                # terminal receipt suppresses target-status publication, so
                # the owner returns 126 rather than claiming target exit 0.
                self.assertEqual(managed.wait(timeout=5), 126)
                self.assertFalse(record.exists())
                terminal = registry / f".{run_id}.terminal"
                self.assertEqual((terminal / "supervisor-ready").read_text(encoding="utf-8"), attestation)
                self.assertTrue((terminal / "signal-delivery").is_file())
                acknowledged = subprocess.run(
                    ["sh", str(helper), "ack-run-terminal", run_id], env=env,
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(acknowledged.returncode, 0, acknowledged.stderr)
                self.assertFalse(terminal.exists())

                stale_id = "stale-owned-run"
                stale_started = root / "stale-target-started"
                stale = subprocess.Popen(["sh", str(helper), "run-owned", stale_id, str(target), str(stale_started)], env=env)
                stale_attestation = None
                try:
                    stale_record = registry / stale_id
                    _wait_for_path(stale_record)
                    _wait_for_path(stale_started)
                    stale_attestation = stale_record.read_text(encoding="utf-8")
                    version, pid, pgid, _marker = stale_attestation.split()
                    stale_record.write_text(f"{version} {pid} {pgid} 202\n", encoding="utf-8")

                    stale_signal = subprocess.run(
                        ["sh", str(helper), "signal-run", stale_id, "TERM"],
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(stale_signal.returncode, 126)
                    self.assertIsNone(stale.poll())
                    self.assertTrue(stale_record.exists())
                    self.assertTrue((registry / f".{stale_id}.lock").is_dir())
                finally:
                    if stale.poll() is None:
                        # Restore only this fixture's deliberately corrupted
                        # attestation before asking the helper to clean up.
                        # Terminating the owner with a stale marker must fail
                        # closed rather than signal an unverified group.
                        if stale_attestation is not None:
                            stale_record.write_text(stale_attestation, encoding="utf-8")
                            cleanup = subprocess.run(
                                ["sh", str(helper), "signal-run", stale_id, "TERM"],
                                env=env, text=True, capture_output=True, check=False,
                            )
                            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
                        else:
                            stale.terminate()
                    self.assertEqual(stale.wait(timeout=5), 126)
                    if stale_attestation is not None:
                        stale_ack = subprocess.run(
                            ["sh", str(helper), "ack-run-terminal", stale_id], env=env,
                            text=True, capture_output=True, check=False,
                        )
                        self.assertEqual(stale_ack.returncode, 0, stale_ack.stderr)
                        self.assertFalse((registry / f".{stale_id}.terminal").exists())
            finally:
                if managed.poll() is None:
                    subprocess.run(["sh", str(helper), "signal-run", run_id, "KILL"], env=env, check=False)
                    managed.wait(timeout=5)

    def test_root_helper_config_zapret_dir_overrides_caller_and_rejects_untrusted_target(self) -> None:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("setsid"):
            self.skipTest("requires a root Linux test environment with setsid")
        helper = Path(__file__).resolve().parents[1] / "scripts" / "gp-root-helper.sh"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trusted_zapret = root / "trusted-zapret"
            caller_zapret = root / "caller-zapret"
            trusted_zapret.mkdir()
            caller_zapret.mkdir()
            trusted_started = root / "trusted-started"
            untrusted_started = root / "untrusted-started"
            trusted_target = trusted_zapret / "blockcheck2.sh"
            trusted_target.write_text('#!/bin/sh\nprintf trusted > "$1"\n', encoding="utf-8")
            trusted_target.chmod(0o700)
            untrusted_target = caller_zapret / "blockcheck2.sh"
            untrusted_target.write_text('#!/bin/sh\nprintf untrusted > "$1"\n', encoding="utf-8")
            untrusted_target.chmod(0o700)
            registry = root / "runs"
            config = root / "gp-root-helper.conf"
            config.write_text(f"ZAPRET_DIR='{_posix_shell_path(trusted_zapret)}'\n", encoding="utf-8")
            env = {
                **os.environ,
                "ZAPRET_DIR": str(caller_zapret),
                "GP_ROOT_HELPER_CONFIG": str(config),
                "GP_ROOT_HELPER_RUN_DIR": str(registry),
            }

            trusted = subprocess.run(
                ["sh", str(helper), "run-owned", "trusted-config-target", str(trusted_target), str(trusted_started)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(trusted.returncode, 0, trusted.stderr)
            self.assertEqual(trusted_started.read_text(encoding="utf-8"), "trusted")

            untrusted_run_id = "caller-config-target"
            untrusted = subprocess.run(
                ["sh", str(helper), "run-owned", untrusted_run_id, str(untrusted_target), str(untrusted_started)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(untrusted.returncode, 126)
            self.assertIn("unsupported run target", untrusted.stderr)
            self.assertFalse(untrusted_started.exists())
            self.assertFalse((registry / untrusted_run_id).exists())
            self.assertFalse((registry / f".{untrusted_run_id}.lock").exists())


def _minimal_multidomain_blockcheck_source(*, wait_for_release: bool = False) -> str:
    wait_body = (
        'printf "started\\n" > "$GP_TEST_STARTED"\n'
        'IFS= read -r _ < "$GP_TEST_RELEASE"\n'
        if wait_for_release
        else ""
    )
    return (
        "#!/bin/sh\n"
        "fsleep_setup() {\n"
        '  printf "%s\\n" "$0" > "$GP_TEST_RUNNER_PATH"\n'
        f"  {wait_body}"
        "}\n"
        "fix_sbin_path() { :; }\n"
        "check_system() { :; }\n"
        "check_already() { :; }\n"
        "require_root() { :; }\n"
        "check_prerequisites() { :; }\n"
        "sigint_cleanup() { :; }\n"
        "check_dns() { :; }\n"
        "check_virt() { :; }\n"
        "ask_params() { :; }\n"
        "sigint() { :; }\n"
        "sigsilent() { :; }\n"
        "configure_ip_version() { :; }\n"
        "cleanup() { :; }\n"
        "UNAME=CYGWIN\n"
        "SKIP_PKTWS=1\n"
        "IPVS=\n"
        "ENABLE_HTTP=0\n"
        "ENABLE_HTTPS_TLS12=0\n"
        "ENABLE_HTTPS_TLS13=0\n"
        "ENABLE_HTTP3=0\n"
        "fsleep_setup\n"
    )


def _run_owned_multidomain_library(
    *,
    shell: str,
    helper: Path,
    source: Path,
    temp_root: Path,
    run_id: str,
    lifecycle: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the production wrapper with its process supervisor replaced by a portable lifecycle driver."""
    with tempfile.TemporaryDirectory() as raw:
        harness_root = Path(raw)
        library = harness_root / "root-helper-library.sh"
        fake_bin = harness_root / "fake-bin"
        fake_bin.mkdir()
        # The dispatch footer invokes require_root, so load only the production function library.
        library.write_text(_root_helper_test_source(helper).split("\nrequire_root\n", 1)[0] + "\n", encoding="utf-8")
        (fake_bin / "rm").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$GP_TEST_RM_LOG\"\n"
            "exec /bin/rm \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin / "rm").chmod(0o700)
        rm_log = (extra_env or {}).get("GP_TEST_RM_LOG", str(harness_root / "rm.log"))
        env = {
            **os.environ,
            **(extra_env or {}),
            "PATH": f"{_posix_shell_path(fake_bin)}:/usr/bin:/bin",
            "TMPDIR": _posix_shell_path(temp_root),
            "ZAPRET_DIR": _posix_shell_path(source.parent),
            "GP_TEST_RM_LOG": rm_log,
        }
        harness = f'''\
. "$1"
run_owned_process() {{
  runner="$2"
{lifecycle}
}}
run_owned_multidomain_target "$2" "$3"
'''
        return subprocess.run(
            [shell, "-c", harness, "owned-multidomain-library", _posix_shell_path(library), run_id, _posix_shell_path(source)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


def _wait_for_path(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"path did not appear: {path}")


def _wait_for_pid_to_exit(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_live(pid):
            return
        time.sleep(0.01)
    raise AssertionError(f"PID {pid} remained live after the managed group was killed")


def _wait_for_proc_path_to_disappear(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    proc_path = Path(f"/proc/{pid}")
    while time.monotonic() < deadline:
        if not proc_path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"PID {pid} remained present in /proc after bounded cleanup")


def _pid_is_live(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        fields = stat_path.read_text(encoding="utf-8").split()
        return len(fields) > 2 and fields[2] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _any_process_carries_argument(argument: str) -> bool:
    """Check the root-helper regression only after its original launcher reaps."""
    completed = subprocess.run(["ps", "-eo", "args="], text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"ps inspection failed: {completed.stderr}")
    return any(argument in line for line in completed.stdout.splitlines())


def _posix_shell() -> str | None:
    shell = shutil.which("sh")
    if shell is not None:
        return shell
    for candidate in (
        Path(r"C:\Program Files\Git\usr\bin\sh.exe"),
        Path(r"C:\Program Files\Git\bin\bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _posix_shell_path(path: Path) -> str:
    value = str(path)
    if os.name != "nt":
        return value
    drive, tail = os.path.splitdrive(value)
    if drive:
        posix_tail = tail.replace("\\", "/")
        return f"/{drive[0].lower()}{posix_tail}"
    return value.replace("\\", "/")


def _native_shell_path(value: str) -> Path:
    if os.name == "nt" and re.fullmatch(r"/[A-Za-z]/.*", value):
        return Path(f"{value[1].upper()}:/{value[3:]}")
    return Path(value)


if __name__ == "__main__":
    unittest.main()
