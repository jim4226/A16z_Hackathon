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
