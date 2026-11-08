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

            def prepare(state_dir, version):
                preparations.append(version)
                return 1

            def launch(update_id, version):
                self.assertEqual(manager.read(update_id)["state"], "running")
                launches.append((update_id, version))
                return done

            manager = CoreUpdate(state, admission, prepare=prepare, launch=launch, probe=lambda: False)
            with admission.claim("search"):
                with self.assertRaises(OperationBusy):
                    manager.start(FIRST, "0.5.0")
            self.assertEqual(preparations, [])
            status, report = manager.start(FIRST, "0.5.0")
            self.assertEqual((status, report["update_id"]), (202, FIRST))
            self.assertEqual(manager.start(FIRST, "0.5.0")[0], 200)
            with self.assertRaisesRegex(ValueError, "другая версия"):
                manager.start(FIRST, "0.5.1")
            self.assertEqual(launches, [(FIRST, "0.5.0")])
            with self.assertRaises(OperationBusy):
                manager.start(SECOND, "0.5.0")
            done.set()
            manager._worker.join(3)
            self.assertFalse(manager._worker.is_alive())
            recovered = CoreUpdate(state, OperationAdmission(), prepare=prepare, launch=launch, probe=lambda: False)
            self.assertEqual(recovered.start(FIRST, "0.5.0")[1]["state"], "unknown")
            unverified = CoreUpdate(state, OperationAdmission(), prepare=prepare,
                                    launch=launch, probe=lambda: None)
            self.assertEqual(unverified.read(FIRST)["state"], "unknown")
            with self.assertRaises(OperationBusy):
                unverified.start(SECOND, "0.5.0")
            self.assertEqual(launches, [(FIRST, "0.5.0")])
            self.assertEqual(preparations, ["0.5.0"])

    def test_delayed_same_id_cannot_start_second_installer_after_first_finishes(self):
        from threading import current_thread

        entered, release = Event(), Event()
        launches = []
        done = Event()
        done.set()

        def prepare(state, version):
            if current_thread().name.startswith("stale-update"):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("stale request was not released")
            return 1

        with TemporaryDirectory() as root:
            state = Path(root)
            manager = CoreUpdate(state, OperationAdmission(), prepare=prepare,
                                 launch=lambda *args: launches.append(args) or done, probe=lambda: False)
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-update") as executor:
                delayed = executor.submit(manager.start, FIRST, "0.5.0")
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(manager.start(FIRST, "0.5.0")[0], 202)
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
            manager = CoreUpdate(Path(root), OperationAdmission(), prepare=lambda *_: 1,
                                 launch=lambda *_: RejectedLaunch(), probe=lambda: False)
            self.assertEqual(manager.start(FIRST, "0.5.0")[0], 202)
            manager._worker.join(3)
            self.assertFalse(manager._worker.is_alive())
            self.assertEqual(manager.read(FIRST)["state"], "failed")
            self.assertEqual(manager.start(FIRST, "0.5.0")[0], 200)
