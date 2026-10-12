"""First-run setup for a downloaded R Cubed release. Uses only the standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".rcubed-venv"
PYTHON = ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MARKER = ENV / "rcubed-installed.json"


def descriptor() -> dict:
    manifest = ROOT / "RELEASE.json"
    if manifest.exists():
        info = json.loads(manifest.read_text(encoding="utf-8"))
        wheel = ROOT / info["wheel"]
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != info["wheel_sha256"]:
            raise RuntimeError("The release wheel is damaged. Download this release again.")
    else:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        info = {"version": re.search(r'^version = "([^"]+)"', project, re.M).group(1)}
    info["lock_sha256"] = hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest()
    return info


def run(*args: str) -> None:
    subprocess.run([str(PYTHON), "-I", *map(str, args)], cwd=ROOT, check=True)


def install(info: dict) -> None:
    if not (3, 10) <= sys.version_info[:2] < (3, 13):
        raise RuntimeError("This beta needs Python 3.10, 3.11 or 3.12 (3.11 recommended).")
    if not PYTHON.exists():
        print("Creating the private R Cubed environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(ENV)
    # Invalidate before any mutation: interrupted/failed setup cannot look complete.
    MARKER.unlink(missing_ok=True)
    run("-m", "ensurepip", "--upgrade")
    print("Installing the pinned release dependencies (internet required for setup)...", flush=True)
    run("-m", "pip", "install", "--require-hashes", "-r", str(ROOT / "requirements.lock"))
    package = ROOT / info["wheel"] if "wheel" in info else ROOT
    run("-m", "pip", "install", "--no-deps", "--no-build-isolation", "--force-reinstall", str(package))
    run("-m", "pip", "check")
    run("-c", "from importlib.metadata import version; "
        f"assert version('morph') == {info['version']!r}; "
        "from morph.resources import asset_path; "
        "assert asset_path('robots','so101','morph_profile.json').is_file(); "
        "import morph.studio.server")
    MARKER.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--setup", action="store_true")
    mode.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    info = descriptor()
    installed = (json.loads(MARKER.read_text(encoding="utf-8"))
                 if not args.setup and MARKER.exists() else None)
    if args.setup or not installed:
        install(info)
    elif installed != info:
        raise RuntimeError("These release files changed. Run setup_studio to install this version explicitly.")
    if args.launch:
        # Isolated mode prevents the source directory or PYTHONPATH shadowing the installed wheel.
        run("-m", "morph.cli", "studio", "--open")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup/startup failed: {exc}", file=sys.stderr)
        sys.exit(1)
