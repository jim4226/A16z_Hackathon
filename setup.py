"""Include external robot resources in wheels without duplicating them in source."""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithResources(build_py):
    def run(self):
        super().run()
        source = Path(__file__).resolve().parent
        target = Path(self.build_lib) / "morph" / "_assets"
        for resource in ("robots", "data/darwin-cache"):
            self.copy_tree(str(source / resource), str(target / resource))
        self.mkpath(str(target))
        self.copy_file(str(source / "LICENSE"), str(target / "LICENSE"))


setup(cmdclass={"build_py": BuildWithResources})
