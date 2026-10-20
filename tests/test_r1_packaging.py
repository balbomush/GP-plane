"""Build hook contract without creating a release wheel or source outputs."""

from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock

try:
    from setuptools import Distribution
    from setuptools.command.build_py import build_py
except ModuleNotFoundError as error:
    if error.name != "setuptools":
        raise
    raise unittest.SkipTest("Build-hook check requires build-system dependency setuptools") from error


class OpenApiBuildTests(unittest.TestCase):
    def test_hook_copies_authoritative_contract_and_declares_build_output(self):
        root = Path(__file__).resolve().parents[1]
        with mock.patch("setuptools.setup") as setup:
            runpy.run_path(str(root / "setup.py"))
        hook = setup.call_args.kwargs["cmdclass"]["build_py"]
        with tempfile.TemporaryDirectory() as temporary:
            command = hook(Distribution())
            command.build_lib = temporary
            command.force = True
            with mock.patch.object(build_py, "run"), mock.patch.object(build_py, "get_outputs", return_value=[]):
                command.run()
                output = Path(temporary) / "gp_control_plane" / "http" / "openapi.json"
                self.assertEqual(output.read_bytes(), (root / "openapi.json").read_bytes())
                self.assertIn(str(output), command.get_outputs())
        self.assertIn("include openapi.json", (root / "MANIFEST.in").read_text())
        self.assertFalse((root / "src" / "gp_control_plane" / "http" / "openapi.json").exists())


if __name__ == "__main__":
    unittest.main()
