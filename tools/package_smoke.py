"""Check an installed Morph wheel from outside the checkout, offline: `python -I tools/package_smoke.py`, run with
the Python the wheel was installed into (CI's package job does this in a fresh venv). -I keeps the checkout off
sys.path, so a pass means the wheel itself carries the Studio, the robot bundles, the Darwin cache and the licences.
"""

import json
import os
import socket
import sys
import tempfile
from importlib import metadata
from pathlib import Path


def _no_network(*args, **kwargs):
    raise AssertionError("the package check must not use the network")


def main() -> int:
    os.environ["MORPH_OFFLINE"] = "1"
    socket.socket.connect = socket.socket.connect_ex = _no_network
    socket.getaddrinfo = _no_network
    start = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="morph-package-check-") as tmp:
        os.chdir(tmp)
        try:
            os.environ["MORPH_HOME"] = os.path.join(tmp, "projects")
            import morph
            from morph import darwin, keys, kicad, resources
            from morph.robot import imported
            from morph.studio import server

            pkg = Path(morph.__file__).resolve().parent
            assert "site-packages" in pkg.parts, f"imported the checkout, not the wheel: {pkg}"
            assert metadata.version("morph") == morph.__version__, (metadata.version("morph"), morph.__version__)
            assert resources.checkout() is None and Path(resources.ROOT) == pkg / "_assets", resources.ROOT
            for rel in ("robots/so101/morph_profile.json", "robots/so101/LICENSE", "robots/so101/NOTICE",
                        "robots/so101/so101_new_calib.urdf", "env.example", "LICENSE", "NOTICE"):
                assert (Path(resources.ROOT) / rel).is_file(), rel
            static = Path(server.STATIC)
            for rel in ("index.html", "app.js", "keys.js", "style.css", "vendor/three.module.min.js", "vendor/THREE_LICENSE",
                        "vendor/fonts/inter-latin-400.woff2", "vendor/fonts/OFL-Inter.txt", "fixtures/robot/robot.glb"):
                assert (static / rel).is_file(), rel
            assert not (static / "viewer_test.html").exists(), "viewer_test.html is a developer page"
            licences = {Path(f).name for f in metadata.distribution("morph").files or [] if "licenses" in str(f)}
            assert {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "THREE_LICENSE", "OFL-Inter.txt"} <= licences, licences
            env = Path(keys.ENV_PATH).resolve()
            assert "site-packages" not in env.parts, f".env would be written into site-packages: {env}"
            saved = os.environ.pop("MORPH_HOME")  # without it: the user's data folder, never ./projects of any folder
            try:
                resources._HOMES.clear()
                projects = resources.projects_home()
                assert projects == os.path.join(resources.data_dir(), "projects") and os.path.isabs(projects), projects
                assert not Path(projects).resolve().is_relative_to(Path(tmp).resolve()), projects
                os.makedirs(os.path.join(tmp, "projects", "thesis"))  # someone's own folder called projects: not Morph's
                resources._HOMES.clear()
                assert resources.projects_home() == projects
                os.makedirs(os.path.join(tmp, "projects", "egg-picker"))  # projects an older install made here: kept
                open(os.path.join(tmp, "projects", "egg-picker", "ledger.jsonl"), "w").close()
                resources._HOMES.clear()
                assert resources.projects_home() == "projects"
            finally:
                os.environ["MORPH_HOME"] = saved
                resources._HOMES.clear()
            profile, folder = imported.load_profile("so101")
            assert Path(folder).resolve().is_relative_to(pkg / "_assets"), folder
            assert profile.get("urdf") and (Path(folder) / profile["urdf"]).is_file()
            darwin.CACHE = os.path.join(tmp, "darwin-cache")  # only the answers shipped in the wheel
            shipped = sorted(Path(darwin.SHIPPED).glob("*.json"))
            assert len(shipped) >= 20, darwin.SHIPPED
            query = json.loads(shipped[0].read_text(encoding="utf-8"))["query"]
            assert darwin.search(query)["cached"] is True
            assert "Apache License" in kicad.license_text("apache-2.0")[1]
        finally:
            os.chdir(start)
    print(f"Morph {morph.__version__} wheel at {pkg}: Studio files, fonts, robot bundles, Darwin cache and licences "
          f"present; .env goes to {env.parent}, projects to {projects} unless MORPH_HOME says otherwise")
    return 0


if __name__ == "__main__":
    sys.exit(main())
