"""Release resource and installer failure regression tests (no package downloads)."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from morph.resources import asset_path


def installer():
    source = Path(__file__).resolve().parents[1] / "tools" / "install_local.py"
    module_spec = importlib.util.spec_from_file_location("release_installer", source)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_resources_reject_parent_escape():
    with pytest.raises(ValueError):
        asset_path("robots", "..", "LICENSE")
    with pytest.raises(FileNotFoundError):
        asset_path("missing-resource.json")


@pytest.mark.parametrize("source", ["../so101", "/so101", "C:\\so101", ".", "a/b"])
def test_robot_resources_reject_arbitrary_paths(source):
    from morph.robot.imported import load_profile
    with pytest.raises(ValueError):
        load_profile(source)


def test_offline_supplier_and_board_requests_do_not_connect(tmp_path, monkeypatch):
    from morph import boardrepo, darwin
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "darwin"))
    monkeypatch.setattr(boardrepo, "CACHE", str(tmp_path / "boards"))
    monkeypatch.setattr(darwin, "urlopen", lambda *a, **k: pytest.fail("Offline supplier request"))
    monkeypatch.setattr(boardrepo, "urlopen", lambda *a, **k: pytest.fail("Offline board request"))
    assert not darwin.search("uncached offline test query")["ok"]
    assert not boardrepo.fetch("test/offline-board")["ok"]


def test_source_descriptor_matches_package_version():
    from morph import __version__
    info = installer().descriptor()
    assert info["version"] == __version__
    assert len(info["lock_sha256"]) == 64


def test_failed_setup_invalidates_previous_ready_marker(tmp_path, monkeypatch):
    module = installer()
    executable = tmp_path / "python"
    executable.touch()
    marker = tmp_path / "rcubed-installed.json"
    marker.write_text('{"version": "old"}')
    monkeypatch.setattr(module, "PYTHON", executable)
    monkeypatch.setattr(module, "MARKER", marker)

    def fail(*args):
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(module, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        module.install({"version": "0.2.0b1"})
    assert not marker.exists()


def test_unchanged_launch_never_runs_setup(tmp_path, monkeypatch):
    module = installer()
    info = {"version": "0.2.0b1", "lock_sha256": "123"}
    marker = tmp_path / "rcubed-installed.json"
    marker.write_text(json.dumps(info))
    monkeypatch.setattr(module, "MARKER", marker)
    monkeypatch.setattr(module, "descriptor", lambda: info)
    monkeypatch.setattr(module.sys, "argv", ["installer", "--launch"])
    calls = []
    monkeypatch.setattr(module, "run", lambda *args: calls.append(args))
    monkeypatch.setattr(module, "install", lambda info: pytest.fail("Launch must not mutate dependencies"))
    module.main()
    assert calls == [("-m", "morph.cli", "studio", "--open")]


def test_changed_release_requires_explicit_setup(tmp_path, monkeypatch):
    module = installer()
    marker = tmp_path / "rcubed-installed.json"
    marker.write_text('{"version": "old"}')
    monkeypatch.setattr(module, "MARKER", marker)
    monkeypatch.setattr(module, "descriptor", lambda: {"version": "new"})
    monkeypatch.setattr(module.sys, "argv", ["installer", "--launch"])
    with pytest.raises(RuntimeError, match="explicitly"):
        module.main()


def test_explicit_setup_repairs_corrupt_marker(tmp_path, monkeypatch):
    module = installer()
    marker = tmp_path / "rcubed-installed.json"
    marker.write_text('{"version":')
    monkeypatch.setattr(module, "MARKER", marker)
    info = {"version": "new"}
    monkeypatch.setattr(module, "descriptor", lambda: info)
    monkeypatch.setattr(module.sys, "argv", ["installer", "--setup"])
    calls = []
    monkeypatch.setattr(module, "install", lambda value: calls.append(value))
    module.main()
    assert calls == [info]
