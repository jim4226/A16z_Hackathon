"""Build a versioned wheel, sdist and source-free local setup bundle (no upload)."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main():
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    subprocess.run([sys.executable, "-m", "build", "--no-isolation"], cwd=ROOT, check=True)
    wheel = DIST / f"morph-{version}-py3-none-any.whl"
    manifest = {"version": version, "wheel": "wheelhouse/" + wheel.name,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    prefix = f"r-cubed-{version}/"
    bundle = DIST / f"r-cubed-{version}-local-setup.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix + "RELEASE.json", json.dumps(manifest, indent=2) + "\n")
        archive.write(wheel, prefix + manifest["wheel"])
        for relative in ("run_studio.bat", "run_studio.sh", "setup_studio.bat", "setup_studio.sh",
                         "tools/install_local.py", "requirements.lock", "LICENSE", "README.md",
                         "CHANGELOG.md", "SECURITY.md", "env.example"):
            archive.write(ROOT / relative, prefix + relative)
        # Ship the linked installation, privacy, integration and release documentation together.
        # Only Markdown is included; credentials and developer state are never globbed into a release.
        for document in sorted((ROOT / "docs").rglob("*.md")):
            archive.write(document, prefix + document.relative_to(ROOT).as_posix())
    files = (wheel, DIST / f"morph-{version}.tar.gz", bundle)
    (DIST / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files), encoding="ascii")
    print(f"Created {bundle.name}. Python 3.10-3.12 must already be installed; setup needs internet.")


if __name__ == "__main__":
    main()
