"""Files Morph ships but never changes: the robot bundles (robots/), Darwin's cached answers (data/darwin-cache/),
env.example and the licence.

In a source checkout (git clone or release zip, the launchers' layout) they sit at the top of the repo. An installed
wheel carries copies under morph/_assets (setup.py puts them there). Projects, keys and caches never go here: where
projects go is projects_home(), and .env's folder config_dir().
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


def data_dir() -> str:
    """The user's own data folder for an installed wheel: %LOCALAPPDATA%\\Morph, ~/Library/Application Support/Morph,
    or $XDG_DATA_HOME/morph (by default ~/.local/share/morph). On Windows and macOS it is config_dir()'s folder."""
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Morph")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Morph")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), "morph")


# what only Morph writes into its projects folder: its own files, and projects (each keeps a ledger.jsonl)
_MORPH_FILES = (".settings.json", ".tools", ".runs", ".team", ".robots", ".imports", ".briefs")


def holds_projects(folder: str) -> bool:
    """True when `folder` already holds Morph's files or projects (a folder with a ledger.jsonl), not just any folder
    called projects."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return False
    if any(n in _MORPH_FILES for n in names):
        return True
    return any(os.path.isfile(os.path.join(folder, n, "ledger.jsonl")) for n in names[:1000])


_HOMES: dict = {}


def projects_home() -> str:
    """Where projects (and their settings, runs, team and tools) go when MORPH_HOME doesn't say: for a checkout,
    `projects` in the Morph folder, as an absolute path, whatever folder Morph starts in (the launchers install a
    `morph` command into .venv that works from any folder); for an installed wheel, `projects` in data_dir(), unless
    ./projects in the current folder already holds Morph projects (an install from before 0.2 made it there), which it
    keeps using. Never a folder of the same name that merely exists: Morph would move into someone's own files."""
    env = os.environ.get("MORPH_HOME")
    if env:
        return env
    root = checkout()
    if root:
        return os.path.join(root, "projects")
    key = os.getcwd()
    if key not in _HOMES:
        _HOMES[key] = "projects" if holds_projects("projects") else os.path.join(data_dir(), "projects")
    return _HOMES[key]


def projects_home_note(home: str) -> str:
    """Why `home` is the projects folder, when that isn't obvious, for `morph doctor` and the Studio's first lines."""
    env = os.environ.get("MORPH_HOME")
    if env:
        return " (MORPH_HOME)" if os.path.abspath(home) == os.path.abspath(env) else ""
    here = os.path.abspath("projects")
    if checkout():                                      # started in another folder that has Morph projects of its own
        if os.path.normcase(os.path.abspath(home)) != os.path.normcase(here) and holds_projects("projects"):
            return (f" (the Morph folder's; the Morph projects in {here} stay where they are, and MORPH_HOME={here} "
                    "opens them)")
        return ""
    if os.path.abspath(home) != here:
        return ""
    return (f" (it already holds Morph projects, so Morph keeps using it; new installs keep projects in "
            f"{os.path.join(data_dir(), 'projects')}, and MORPH_HOME chooses another folder)")
