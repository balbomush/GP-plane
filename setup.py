"""Copy the sole source OpenAPI contract into the built package."""

from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / "gp_control_plane" / "http" / "openapi.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        self.copy_file(str(Path(__file__).parent / "openapi.json"), str(target))

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + [
            str(Path(self.build_lib) / "gp_control_plane" / "http" / "openapi.json")
        ]


setup(cmdclass={"build_py": BuildPy})
