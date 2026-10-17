"""Builds put the files Morph reads from the top of a checkout (robots/, data/darwin-cache/, env.example and the
licence) into morph/_assets inside the wheel, so an installed copy finds them (morph/resources.py). The checkout
itself keeps its layout, and editable installs (pip install -e .) read them where they are."""

import os

from setuptools import setup
from setuptools.command.build_py import build_py

ASSETS = ("robots", "data/darwin-cache", "env.example", "LICENSE", "NOTICE")


class BuildWithAssets(build_py):
    def run(self):
        super().run()
        if getattr(self, "editable_mode", False):
            return
        here = os.path.dirname(os.path.abspath(__file__))
        target = os.path.join(self.build_lib, "morph", "_assets")
        for rel in ASSETS:
            src, dst = os.path.join(here, *rel.split("/")), os.path.join(target, *rel.split("/"))
            if os.path.isdir(src):
                self.copy_tree(src, dst)
            else:
                self.mkpath(os.path.dirname(dst))
                self.copy_file(src, dst)


setup(cmdclass={"build_py": BuildWithAssets})
