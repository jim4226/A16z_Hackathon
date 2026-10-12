"""Run with `python -I tools/package_smoke.py` after installing the built wheel.

Fails if imports accidentally use the checkout; no API key or provider call needed.
"""
from contextlib import ExitStack
from importlib import metadata, resources
import json
import os
from pathlib import Path
import socket
import tempfile


def main():
    os.environ["MORPH_OFFLINE"] = "1"

    def forbidden(*args, **kwargs):
        raise AssertionError("Installed-wheel smoke must not access the network")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.getaddrinfo = forbidden
    with tempfile.TemporaryDirectory(prefix="rcubed-package-smoke-") as directory, ExitStack() as cleanup:
        cleanup.callback(os.chdir, os.getcwd())
        os.chdir(directory)
        os.environ["MORPH_HOME"] = str(Path(directory) / "projects")
        os.environ["MORPH_CONFIG_DIR"] = str(Path(directory) / "config")
        import morph
        from morph import darwin, kicad, spec
        from morph.resources import asset_path
        from morph.robot import imported, pipeline2, timestudy2
        from morph.robot.urdf_import import load_urdf
        from morph.studio import server

        assert "site-packages" in str(Path(morph.__file__).resolve()), morph.__file__
        assert metadata.version("morph") == morph.__version__
        static = resources.files("morph.studio") / "static"
        for name in ("index.html", "app.js", "style.css", "vendor/three.module.min.js",
                     "vendor/THREE_LICENSE", "fixtures/robot/robot.glb"):
            assert (static / name).is_file(), name
        assert Path(server.STATIC).joinpath("index.html").is_file()
        assert Path(timestudy2.BASELINE_PATH).is_file()
        assert asset_path("LICENSE").is_file()
        assert asset_path("robots", "so101", "LICENSE").is_file()
        assert asset_path("robots", "so101", "NOTICE").is_file()
        profile, folder = imported.load_profile("so101")
        assembly = load_urdf(str(Path(folder) / profile["urdf"]), profile)
        assert len(assembly.parts) >= 10
        assert all(len(part.mesh.vertices) for part in assembly.parts)
        assert len(pipeline2.pipeline_version()) == 12
        cache = next(Path(darwin.SHIPPED).glob("*.json"))
        query = json.loads(cache.read_text(encoding="utf-8"))["query"]
        assert darwin.search(query)["cached"] is True
        # Exercise actual exported geometry with declared SciPy and manifold dependencies.
        from morph import design
        mesh = design.export_stl(design.build_profile(spec.Design()), str(Path(directory) / "finger.stl"))
        assert mesh["watertight"]
        print(f"Installed R Cubed {morph.__version__}: UI, CAD, robot meshes, licenses and offline cache OK.")


if __name__ == "__main__":
    main()
