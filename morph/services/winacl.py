"""Windows: limit a folder or file that Morph keeps private to the user running Morph.

On macOS and Linux Morph's private files (.env, agent runs, the tools' and the team's files) are 0600 and their folders
0700. Windows ignores those modes: there a file is as private as the folder it is in, through the permissions it
inherits. In the user's own folders (Documents, the Desktop) that means the user, SYSTEM and the administrators, but in
a folder such as C:\\Morph it can mean every user of the computer.

restrict() asks icacls to give this user full control of the path and then to drop the permissions it inherits, so it
(and, for a folder, every file made in it later) belongs to this user only. The grant comes first and the inherited
permissions go only once it succeeded, so a failure can't lock the user out. It never raises: when icacls is missing,
refuses or takes too long, nothing changes and the path stays as private as its folder, as before. Standard library
only; on other systems it does nothing.
"""

from __future__ import annotations

import os
import subprocess

TIMEOUT_S = 15


def _user(env) -> str | None:
    """DOMAIN\\name of the user running Morph (USERDOMAIN is the computer's name for a local account), or None."""
    name, domain = (env.get("USERNAME") or "").strip(), (env.get("USERDOMAIN") or "").strip()
    if not name or any(c in name + domain for c in ':"\r\n'):
        return None
    return f"{domain}\\{name}" if domain else name


def restrict(path: str, *, run=subprocess.run, nt: bool | None = None, env=None) -> bool:
    """True when `path` now belongs to this user only (Windows); False when nothing was changed."""
    if not (os.name == "nt" if nt is None else nt):
        return False
    user = _user(os.environ if env is None else env)
    if not user or not os.path.exists(path):
        return False
    grant = f"{user}:(OI)(CI)F" if os.path.isdir(path) else f"{user}:F"
    kw = {"capture_output": True, "timeout": TIMEOUT_S, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        if run(["icacls", path, "/grant:r", grant, "/Q"], **kw).returncode != 0:
            return False
        return run(["icacls", path, "/inheritance:r", "/Q"], **kw).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
