"""Update acceptance persists correlation before invoking the fixed launcher."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from gp_control_plane.application.core_update import CoreUpdate
from gp_control_plane.application.operation_admission import OperationAdmission, OperationBusy


FIRST = "e9eb11ed-7f79-4cb9-9ba8-dc77414f086b"
SECOND = "d790f561-275a-47e3-b36f-a55a7b53b1df"


class UpdateTests(unittest.TestCase):
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
            self.assertEqual(launches, [(FIRST, "0.5.0")])
            self.assertEqual(preparations, ["0.5.0"])

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
