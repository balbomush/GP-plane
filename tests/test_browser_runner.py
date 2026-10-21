from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.browser import runner
from tests.browser.runner import PlaywrightPage


class PlaywrightRunnerLifecycleTests(unittest.TestCase):
    def test_shutdown_preserves_primary_error_and_removes_profile_after_repeated_wait_failures(self) -> None:
        class BrokenStream:
            def close(self) -> None:
                raise OSError("forced stream close failure")

            def read(self) -> str:
                raise OSError("forced stream read failure")

        class BrokenProcess:
            stdin = BrokenStream()
            stdout = BrokenStream()
            stderr = BrokenStream()
            returncode = 9

            def poll(self) -> None:
                return None

            def wait(self, timeout: float) -> None:
                raise subprocess.TimeoutExpired("bridge", timeout)

            def kill(self) -> None:
                raise OSError("forced kill failure")

        with tempfile.TemporaryDirectory() as raw:
            page = object.__new__(PlaywrightPage)
            page._closed = False
            page._process = BrokenProcess()
            page._profile_dir = Path(raw) / "profile"
            page._profile_dir.mkdir()
            page._artifact_dir = Path(raw) / "artifacts"
            page._call = Mock(side_effect=AssertionError("forced bridge close failure"))
            primary = AssertionError("primary browser assertion")
            self.assertIsNone(page.__exit__(AssertionError, primary, None))
            self.assertFalse(page._profile_dir.exists())
            notes = "\n".join(getattr(primary, "__notes__", ()))
            self.assertIn("bridge wait", notes)
            self.assertIn("bridge wait after kill", notes)
            self.assertIn("bridge kill", notes)

    def test_runner_script_disables_bytecode_writes(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run-browser-tests.ps1").read_text(encoding="utf-8")
        self.assertIn("& $python -B -m unittest @tests", script)

    def test_installer_reads_authoritative_toolchain_lock(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "install-browser-runner.ps1").read_text(encoding="utf-8")
        self.assertIn("toolchain.lock.json", script)
        self.assertIn("$lock.node.version", script)
        self.assertIn("$lock.playwright.version", script)
        self.assertIn("$lock.browser.revision", script)
        self.assertNotIn("nodeVersion = '24.19.0'", script)

    def test_toolchain_lock_matches_runtime_validation(self) -> None:
        lock = runner._load_toolchain_lock()
        runtime = runner._runtime_path(lock)
        node, module = runner._validate_toolchain(runtime, lock)
        self.assertTrue(node.is_file())
        self.assertEqual(__import__("json").loads(module.read_text(encoding="utf-8"))["version"], lock["playwright"]["version"])

    def test_explicit_runtime_does_not_bypass_toolchain_validation(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(runner.os.environ,{'GP_BROWSER_RUNTIME':folder}):
            lock=runner._load_toolchain_lock()
            self.assertEqual(runner._runtime_path(lock),Path(folder).resolve())
            with self.assertRaisesRegex(AssertionError,'pinned Node is unavailable'):
                runner._validate_toolchain(runner._runtime_path(lock),lock)

    def test_success_cleans_isolated_profile_and_artifacts(self) -> None:
        with PlaywrightPage() as page:
            artifact_dir = page._artifact_dir
            profile_dir = page._profile_dir
            page.navigate("data:text/html,<main>ready</main>")
            page.wait_for("document.querySelector('main').textContent === 'ready'", "page became ready")
        self.assertFalse(profile_dir.exists())
        self.assertFalse(artifact_dir.exists())
        self.assertIsNotNone(page._process.returncode)

    def test_click_uses_playwright_actionability_checks(self) -> None:
        artifact_dir = None
        try:
            with PlaywrightPage() as page:
                artifact_dir = page._artifact_dir
                page.navigate("data:text/html,<input id=login value=''><button id=covered hidden>Retry</button><button id=submit disabled>Submit</button>")
                page.fill("#login", "admin")
                self.assertEqual(page.evaluate("document.getElementById('login').value"), "admin")
                with self.assertRaisesRegex(AssertionError, "Playwright failure"):
                    page.click("#covered", timeout=0.1)
                with self.assertRaisesRegex(AssertionError, "Playwright failure"):
                    page.click("#submit", timeout=0.1)
        finally:
            if artifact_dir is not None and artifact_dir.exists():
                shutil.rmtree(artifact_dir)

    def test_browser_command_failure_preserves_complete_evidence_and_reaps_bridge(self) -> None:
        artifact_dir = None
        profile_dir = None
        page = None
        try:
            with PlaywrightPage() as opened:
                page = opened
                artifact_dir = opened._artifact_dir
                profile_dir = opened._profile_dir
                opened.navigate("data:text/html,<main>failure-fixture</main>")
                opened.wait_for("document.querySelector('main').textContent === 'failure-fixture'", "failure fixture became ready")
                opened._call("forced-failure")
        except AssertionError as error:
            self.assertIn("unknown bridge command", str(error))
        else:
            self.fail("the bridge accepted an unknown command")
        assert artifact_dir is not None and profile_dir is not None and page is not None
        try:
            for filename in ("bridge-error.txt", "console.log", "network.log", "failure.png", "trace.zip"):
                self.assertTrue((artifact_dir / filename).is_file(), filename)
            self.assertTrue(list(artifact_dir.glob("*.webm")), "failure video was not finalized")
            self.assertFalse(profile_dir.exists())
            self.assertIsNotNone(page._process.returncode)
        finally:
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)

    def test_shutdown_is_idempotent_after_open(self) -> None:
        page = PlaywrightPage()
        try:
            page.__enter__()
            self.assertEqual(page._shutdown(), [])
            self.assertEqual(page._shutdown(), [])
            self.assertIsNotNone(page._process.returncode)
        finally:
            page.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
