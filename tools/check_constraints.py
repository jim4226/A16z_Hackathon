"""Check that constraints.txt installs from wheels alone (nothing to compile) for every supported Python on every
supported OS, by resolving requirements.txt (and the optional extras) for each combination with uv:

    python tools/check_constraints.py            # the launchers' install: requirements.txt
    python tools/check_constraints.py --extras   # also morph[video], morph[cad], morph[xacro] and CI's tools

Needs uv (https://docs.astral.sh/uv/) and network access to PyPI; it reads package metadata, nothing is installed.
To refresh the pins (then run this again, and the full CI matrix):

    uv pip compile requirements.txt tools/constraints.in --universal --python-version 3.10 \\
        --exclude-newer <today> --no-annotate --no-header -o constraints.txt   (then restore the header)
"""

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
# (label, uv --python-platform, macOS version the wheels must support)
PLATFORMS = (("Windows x64", "x86_64-pc-windows-msvc", None), ("macOS 12+ arm64", "aarch64-apple-darwin", "12.0"),
             ("Linux x64 glibc 2.28+", "x86_64-manylinux_2_28", None))


def resolve(uv: str, inputs: list, platform: str, python: str, macos: str | None) -> str:
    """'' when every package resolves to a wheel for that combination, else uv's reason."""
    env = dict(os.environ, **({"MACOSX_DEPLOYMENT_TARGET": macos} if macos else {}))
    cmd = [uv, "pip", "compile", *inputs, "-c", os.path.join(ROOT, "constraints.txt"), "--python-platform", platform,
           "--python-version", python, "--only-binary", ":all:", "--no-header", "-q", "-o", os.devnull]
    run = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if run.returncode == 0:
        return ""
    lines = [ln.strip() for ln in (run.stderr or run.stdout).splitlines() if "Because" in ln or "error" in ln.lower()]
    return lines[0] if lines else (run.stderr or run.stdout).strip()[-200:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--extras", action="store_true", help="also check the optional extras and CI's tools")
    a = ap.parse_args()
    uv = shutil.which("uv")
    if not uv:
        print("uv is not installed: see https://docs.astral.sh/uv/getting-started/installation/")
        return 2
    inputs = [os.path.join(ROOT, "requirements.txt")] + ([os.path.join(ROOT, "tools", "constraints.in")] if a.extras else [])
    failed = 0
    for label, platform, macos in PLATFORMS:
        for python in PYTHONS:
            why = resolve(uv, inputs, platform, python, macos)
            failed += bool(why)
            print(f"{label:<22} Python {python:<5} {'ok' if not why else 'FAILS: ' + why}", flush=True)
    print("\nEvery combination installs from wheels." if not failed else f"\n{failed} combination(s) fail.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
