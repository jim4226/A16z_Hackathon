"""Files Morph ships but never changes: the robot bundles (robots/), Darwin's cached answers (data/darwin-cache/),
env.example and the licence.

In a source checkout (git clone or release zip, the launchers' layout) they sit at the top of the repo. An installed
wheel carries copies under morph/_assets (setup.py puts them there). Projects, keys and caches never go here.
"""

from __future__ import annotations

import os
import sys

PACKAGE = os.path.dirname(os.path.abspath(__file__))
BUNDLED = os.path.join(PACKAGE, "_assets")
ROOT = BUNDLED if os.path.isdir(BUNDLED) else os.path.dirname(PACKAGE)


def path(*parts: str) -> str:
    """A shipped file or folder, e.g. path("robots") or path("data", "darwin-cache"). It may not exist."""
    return os.path.join(ROOT, *parts)


def checkout() -> str | None:
    """The folder this copy of Morph runs from (where run_studio.bat lives), or None for an installed wheel."""
    return None if ROOT == BUNDLED else ROOT


def _launcher_venv() -> str | None:
    """The launchers' .venv in this checkout when it is the Python running Morph, else None."""
    root = checkout()
    if not root:
        return None
    venv = os.path.join(root, ".venv")
    same = os.path.normcase(os.path.realpath(sys.prefix)) == os.path.normcase(os.path.realpath(venv))
    return venv if same else None


def morph_command() -> str:
    """How a terminal in the Morph folder runs Morph. The launchers install a morph command into .venv, which works
    without activating .venv when named by its path; Morph installed any other way is `morph`."""
    venv = _launcher_venv()
    if not venv:
        return "morph"
    sub = "Scripts" if os.name == "nt" else "bin"
    if os.path.exists(os.path.join(venv, sub, "morph.exe" if os.name == "nt" else "morph")):
        return os.path.join(".venv", sub, "morph")
    return os.path.join(".venv", sub, "python") + " -m morph.cli"


def pip_install_command(packages: str) -> str:
    """The command that adds optional packages (e.g. "imageio-ffmpeg") to the Python running Morph, for a terminal in
    the Morph folder: never `pip install morph[...]`, which would fetch an unrelated project from PyPI."""
    if _launcher_venv():
        py = os.path.join(".venv", "Scripts" if os.name == "nt" else "bin", "python")
        return f"{py} -m pip install {packages} -c constraints.txt"
    return f"python -m pip install {packages}"


def config_dir() -> str:
    """Where .env lives: next to the launchers in a checkout; for an installed wheel, the user's own settings folder
    (never site-packages)."""
    root = checkout()
    if root:
        return root
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Morph")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Morph")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "morph")
