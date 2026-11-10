"""Update acceptance persists correlation before invoking the fixed launcher."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from subprocess import CompletedProcess, TimeoutExpired
from threading import Event
import unittest
from unittest.mock import patch

from gp_control_plane.application.core_update import CoreUpdate, _probe_scope
from gp_control_plane.application.operation_admission import OperationAdmission, OperationBusy


FIRST = "e9eb11ed-7f79-4cb9-9ba8-dc77414f086b"
SECOND = "d790f561-275a-47e3-b36f-a55a7b53b1df"


class UpdateTests(unittest.TestCase):
    def test_scope_discovery_distinguishes_active_absent_and_unverified(self):
        for output, expected in (
            ("LoadState=loaded\nActiveState=active\n", True),
            ("LoadState=not-found\nActiveState=inactive\n", False),
            ("LoadState=loaded\nActiveState=failed\n", False),
            ("", None),
        ):
            with self.subTest(output=output), patch(
                "gp_control_plane.application.core_update.sys.platform", "linux"
            ), patch("gp_control_plane.application.core_update.subprocess.run",
                     return_value=CompletedProcess([], 0, output, "")):
                self.assertIs(_probe_scope(), expected)
        with patch("gp_control_plane.application.core_update.sys.platform", "linux"), patch(
            "gp_control_plane.application.core_update.subprocess.run", side_effect=TimeoutExpired([], 3)
        ):
            self.assertIsNone(_probe_scope())

    def test_durable_replay_and_busy_before_preparation(self):
        with TemporaryDirectory() as root:
            state = Path(root)
            admission = OperationAdmission()
            launches = []
            preparations = []
            done = Event()

            def prepare(state_dir, version, tag):
                preparations.append(version)
                return {"version": version, "tag": tag, "directory": "bound-B"}

            def launch(update_id, version):
                self.assertEqual(manager.read(update_id)["state"], "running")
                launches.append((update_id, version))
                return done

            manager = CoreUpdate(state, admission, prepare=prepare, launch=launch, probe=lambda: False)
            with admission.claim("search"):
                with self.assertRaises(OperationBusy):
                    manager.start(FIRST, "0.5.0", "alpha")
            self.assertEqual(preparations, [])
            status, report = manager.start(FIRST, "0.5.0", "alpha")
            self.assertEqual((status, report["update_id"]), (202, FIRST))
            self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 200)
            with self.assertRaisesRegex(ValueError, "другая версия"):
                manager.start(FIRST, "0.5.1", "alpha")
            self.assertEqual(launches, [(FIRST, "0.5.0")])
            with self.assertRaisesRegex(ValueError, "стадия"):
                manager.start(FIRST, "0.5.0", "stable")
            with self.assertRaises(OperationBusy):
                manager.start(SECOND, "0.5.0", "alpha")
            done.set()
            manager._worker.join(3)
            self.assertFalse(manager._worker.is_alive())
            recovered = CoreUpdate(state, OperationAdmission(), prepare=prepare, launch=launch, probe=lambda: False)
            self.assertEqual(recovered.start(FIRST, "0.5.0", "alpha")[1]["state"], "unknown")
            unverified = CoreUpdate(state, OperationAdmission(), prepare=prepare,
                                    launch=launch, probe=lambda: None)
            self.assertEqual(unverified.read(FIRST)["state"], "unknown")
            with self.assertRaises(OperationBusy):
                unverified.start(SECOND, "0.5.0", "alpha")
            self.assertEqual(launches, [(FIRST, "0.5.0")])
            self.assertEqual(preparations, ["0.5.0"])

    def test_delayed_same_id_cannot_start_second_installer_after_first_finishes(self):
        from threading import current_thread

        entered, release = Event(), Event()
        launches = []
        done = Event()
        done.set()

        def prepare(state, version, tag):
            if current_thread().name.startswith("stale-update"):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("stale request was not released")
            return {"version": version, "tag": tag, "directory": "bound-B"}

        with TemporaryDirectory() as root:
            state = Path(root)
            manager = CoreUpdate(state, OperationAdmission(), prepare=prepare,
                                 launch=lambda *args: launches.append(args) or done, probe=lambda: False)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-update") as executor:
                delayed = executor.submit(manager.start, FIRST, "0.5.0", "alpha")
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 202)
                    manager._worker.join(3)
                    self.assertFalse(manager._worker.is_alive())
                finally:
                    release.set()
                self.assertEqual(delayed.result(5)[0], 200)
            self.assertEqual(launches, [(FIRST, "0.5.0")])

    def test_confirmed_launcher_failure_leaves_a_terminal_report(self):
        class RejectedLaunch:
            def wait(self):
                return 1
        with TemporaryDirectory() as root:
            manager = CoreUpdate(Path(root), OperationAdmission(), prepare=lambda *_: {"directory": "test"},
                                 launch=lambda *_: RejectedLaunch(), probe=lambda: False)
            self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 202)
            manager._worker.join(3)
            self.assertFalse(manager._worker.is_alive())
            self.assertEqual(manager.read(FIRST)["state"], "failed")
            self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 200)

    def test_update_id_keeps_original_supply_after_later_download_and_stage_conflicts(self):
        import json
        from gp_control_plane import core_supply, releases
        from tests.test_core_release_selection import row
        from tests.test_api_v2_service import DELIVERY
        with TemporaryDirectory() as root:
            state = Path(root)
            with patch.object(releases, "fetch_github_releases", return_value=json.dumps([row(2)])), patch.object(
                releases, "fetch_release_asset", side_effect=lambda t,n,s: DELIVERY[n]
            ):
                second = core_supply.prepare(state, "0.5.0", "alpha")
            done = Event()
            done.set()
            launches = []
            manager = CoreUpdate(state, OperationAdmission(), launch=lambda *a: launches.append(a) or done, probe=lambda: False)
            with patch.object(releases, "fetch_github_releases", side_effect=OSError("offline")) as network:
                self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 202)
                manager._worker.join(3)
                network.assert_not_called()
            saved = json.loads(manager.path.read_text())
            self.assertEqual(saved["supply"], second)
            with patch.object(releases, "fetch_github_releases", return_value=json.dumps([row(3)])), patch.object(
                releases, "fetch_release_asset", side_effect=lambda t,n,s: DELIVERY[n]
            ):
                core_supply.prepare(state, "0.5.0", "alpha")
            self.assertEqual(manager.start(FIRST, "0.5.0", "alpha")[0], 200)
            self.assertEqual(json.loads(manager.path.read_text())["supply"], second)
            with self.assertRaisesRegex(ValueError, "стадия"):
                manager.start(FIRST, "0.5.0", "stable")
            self.assertEqual(len(launches), 1)

    def test_downgrade_is_rejected_before_preparation(self):
        with TemporaryDirectory() as root:
            calls = []
            manager = CoreUpdate(Path(root), OperationAdmission(), prepare=lambda *a: calls.append(a), probe=lambda: False)
            with self.assertRaisesRegex(ValueError, "Понижение"):
                manager.start(FIRST, "0.4.4", "stable")
            self.assertEqual(calls, [])

    def test_search_winning_final_admission_prevents_installer(self):
        ready, release = Event(), Event()
        def prepare(*args):
            ready.set()
            self.assertTrue(release.wait(5))
            return {"directory": "B"}
        with TemporaryDirectory() as root, ThreadPoolExecutor(max_workers=1) as pool:
            launches = []
            admission = OperationAdmission()
            manager = CoreUpdate(Path(root), admission, prepare=prepare, launch=lambda *a: launches.append(a), probe=lambda: False)
            result = pool.submit(manager.start, FIRST, "0.5.0", "alpha")
            self.assertTrue(ready.wait(3))
            with admission.claim("search"):
                release.set()
                with self.assertRaises(OperationBusy):
                    result.result(5)
            self.assertEqual(launches, [])
            self.assertFalse(manager.path.exists())
