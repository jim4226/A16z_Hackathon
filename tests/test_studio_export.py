"""Exports contain a bounded engineering revision, never app secrets/history."""

import json
from pathlib import Path
import zipfile

import pytest

from morph.robot.pipeline2 import RobotProject
from morph.studio import export


@pytest.fixture
def project(tmp_path):
    project = RobotProject(str(tmp_path / "egg-picker"))
    revision = Path(project.root) / "r001"
    revision.mkdir()
    (revision / "design.json").write_text('{"name":"egg-picker"}', encoding="utf-8")
    (revision / "results.json").write_text(json.dumps({
        "stage_status": {"cad": "computed", "wiring": "error"},
        "summary": {"feasible": False}, "electronics": {"firmware_validation": {
            "status": "unverified", "board_core": "not_verified", "bench": "not_verified"}},
    }), encoding="utf-8")
    (revision / "cad").mkdir()
    (revision / "cad" / "robot.glb").write_bytes(b"demo CAD bytes")
    return project


def test_export_contains_manifest_with_truthful_validation(project):
    stream, filename = export.build_export(project, "r001")
    try:
        assert filename == "egg-picker-r001.zip"
        with zipfile.ZipFile(stream) as bundle:
            manifest = json.loads(bundle.read("export-manifest.json"))
            assert manifest["project"] == "egg-picker" and manifest["revision"] == "r001"
            assert manifest["stage_status"]["wiring"] == "error"
            assert manifest["stage_status"]["sim"] == "unknown"
            assert manifest["summary"]["feasible"] is False
            assert manifest["firmware_validation"]["bench"] == "not_verified"
            assert bundle.read("cad/robot.glb") == b"demo CAD bytes"
    finally:
        stream.close()


def test_export_ignores_secrets_history_hidden_and_unrelated_files(project):
    root = Path(project.root)
    revision = root / "r001"
    secret = "sk-ant-not-a-real-key-do-not-export"
    for relative in [".env", "settings.json", "ledger.jsonl", "chat-history.json", "r001/.env",
                     "r001/settings.json", "r001/chat.json", "r001/cad/api-key.txt", "r001/cad/credentials.json",
                     "r001/cad/private.pem", "r001/cad/.hidden.json", "r001/cad/id_rsa"]:
        (root / relative).write_text(secret, encoding="utf-8")
    (revision / "private").mkdir()
    (revision / "private" / "config.json").write_text(secret, encoding="utf-8")
    stream, _ = export.build_export(project, "r001")
    try:
        with zipfile.ZipFile(stream) as bundle:
            assert set(bundle.namelist()) == {"export-manifest.json", "design.json", "results.json", "cad/robot.glb"}
            assert all(secret.encode() not in bundle.read(name) for name in bundle.namelist())
    finally:
        stream.close()


@pytest.mark.parametrize("revision", ["../r001", "r001/../../private", "C:/secret", "/r001", "r999", None])
def test_export_rejects_arbitrary_paths(project, revision):
    with pytest.raises(export.ExportError, match="Unknown revision"):
        export.build_export(project, revision)


def test_export_size_and_count_are_bounded(project, monkeypatch):
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 50)
    with pytest.raises(export.ExportError, match="exceeds"):
        export.build_export(project, "r001")
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 200 * 1024 * 1024)
    monkeypatch.setattr(export, "MAX_EXPORT_FILES", 3)
    with pytest.raises(export.ExportError, match="exceeds"):
        export.build_export(project, "r001")


def test_export_skips_symlink_to_private_file(project, tmp_path):
    outside = tmp_path / "private.json"
    outside.write_text("sk-ant-not-a-real-key", encoding="utf-8")
    link = Path(project.root) / "r001" / "cad" / "unexpected.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privilege")
    stream, _ = export.build_export(project, "r001")
    try:
        with zipfile.ZipFile(stream) as bundle:
            assert "cad/unexpected.json" not in bundle.namelist()
    finally:
        stream.close()


def test_revision_symlink_escape_is_rejected(project, tmp_path):
    outside = tmp_path / "private-project"
    outside.mkdir()
    link = Path(project.root) / "r002"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or privilege")
    with pytest.raises(export.ExportError, match="Invalid revision path"):
        export.build_export(project, "r002")


def test_old_revision_defaults_to_unverified_firmware(project):
    (Path(project.root) / "r001" / "results.json").write_text('{"summary":{"feasible":true}}', encoding="utf-8")
    stream, _ = export.build_export(project, "r001")
    try:
        with zipfile.ZipFile(stream) as bundle:
            manifest = json.loads(bundle.read("export-manifest.json"))
            assert manifest["firmware_validation"]["status"] == "unverified"
            assert set(manifest["stage_status"].values()) == {"unknown"}
    finally:
        stream.close()
