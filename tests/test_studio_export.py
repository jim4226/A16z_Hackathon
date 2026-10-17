"""Exports hold a bounded engineering revision (or a project's design history) with a manifest that tells the
truth about each stage and the firmware, never app secrets, chat or the ledger. Offline."""

import hashlib
import http.client
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import ORDER, RobotProject
from morph.spec import Design
from morph.studio import export, server


def _rev(project, rev, results, files=()):
    folder = Path(project.root) / rev
    folder.mkdir()
    (folder / "design.json").write_text('{"name":"egg-picker"}', encoding="utf-8")
    (folder / "results.json").write_text(json.dumps(results), encoding="utf-8")
    for rel, data in files:
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)


@pytest.fixture
def project(tmp_path):
    project = RobotProject(str(tmp_path / "egg-picker"))
    (Path(project.root) / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": "eggs",
                                                              "token": "sk-ant-not-a-real-key"}), encoding="utf-8")
    _rev(project, "r001", {"stage_status": {"cad": "computed", "wiring": "error"}, "errors": {"wiring": "no board"},
                           "summary": {"feasible": False}, "label": "first design",
                           "electronics": {"firmware_validation": {"status": "unverified", "board_core": "not_verified",
                                                                   "bench": "not_verified"}}},
         [("cad/robot.glb", b"demo CAD bytes")])
    return project


def _open(stream):
    try:
        return zipfile.ZipFile(io.BytesIO(stream.read()))
    finally:
        stream.close()


def test_revision_export_has_a_truthful_manifest(project):
    stream, filename = export.build_export(project, "r001")
    assert filename == "egg-picker-r001.zip"
    bundle = _open(stream)
    manifest = json.loads(bundle.read("export-manifest.json"))
    assert manifest["kind"] == "revision" and manifest["project"] == "egg-picker" and manifest["revision"] == "r001"
    assert manifest["stage_status"]["wiring"] == "error" and manifest["stage_status"]["sim"] == "unknown"
    assert manifest["stage_problems"]["wiring"] == "no board" and "cad" in manifest["stage_problems"]  # no CAD output
    assert manifest["feasible"] is False and manifest["firmware_validation"]["bench"] == "not_verified"
    assert "not bench-tested" in manifest["note"]
    assert bundle.read("cad/robot.glb") == b"demo CAD bytes"
    listed = {f["path"]: f for f in manifest["files"]}
    assert listed["cad/robot.glb"]["sha256"] == hashlib.sha256(b"demo CAD bytes").hexdigest()
    assert set(listed) | {"export-manifest.json"} == set(bundle.namelist())


def test_secrets_history_hidden_and_unrelated_files_stay_out(project):
    root = Path(project.root)
    secret = "sk-ant-not-a-real-key-do-not-export"
    for rel in (".env", "settings.json", "ledger.jsonl", "chat-history.json", ".settings.json", "r001/.env",
                "r001/settings.json", "r001/chat.json", "r001/ledger.jsonl", "r001/cad/api-key.txt", "r001/cad/credentials.json",
                "r001/cad/private.pem", "r001/cad/.hidden.json", "r001/cad/id_rsa", "r001/cad/run.log", "r001/notes.txt"):
        (root / rel).write_text(secret, encoding="utf-8")
    for folder in ("r001/private", "r001/.git", "r001/bench"):
        (root / folder).mkdir()
        (root / folder / "config.json").write_text(secret, encoding="utf-8")
    bundle = _open(export.build_export(project, "r001")[0])
    assert set(bundle.namelist()) == {"export-manifest.json", "design.json", "results.json", "cad/robot.glb"}
    assert all(secret.encode() not in bundle.read(name) for name in bundle.namelist())


@pytest.mark.parametrize("rev", ["../r001", "r001/../../private", "C:/secret", "/r001", "r999", "r001/cad", "", None, 1])
def test_only_known_revisions_export(project, rev):
    with pytest.raises(export.ExportError) as exc:
        export.build_export(project, rev)
    assert exc.value.status == 404


def test_a_revision_still_being_built_is_refused(project):
    (Path(project.root) / "r002").mkdir()
    (Path(project.root) / "r002" / "design.json").write_text("{}", encoding="utf-8")
    with pytest.raises(export.ExportError, match="still being built") as exc:
        export.build_export(project, "r002")
    assert exc.value.status == 409


def test_size_and_count_are_bounded(project, monkeypatch):
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 50)
    with pytest.raises(export.ExportError, match="larger than") as exc:
        export.check(project, "r001")
    assert exc.value.status == 413
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 200 * 1024 * 1024)
    for i in range(5):
        (Path(project.root) / "r001" / "cad" / f"part{i}.stl").write_bytes(b"x")
    monkeypatch.setattr(export, "MAX_EXPORT_FILES", 3)
    with pytest.raises(export.ExportError, match="more than"):
        export.build_export(project, "r001")


def test_symlinks_never_leave_the_revision(project, tmp_path):
    outside = tmp_path / "private.json"
    outside.write_text("sk-ant-not-a-real-key", encoding="utf-8")
    (tmp_path / "private-project").mkdir()
    try:
        os.symlink(outside, Path(project.root) / "r001" / "cad" / "unexpected.json")
        os.symlink(tmp_path / "private-project", Path(project.root) / "r002", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights on this system")
    assert "cad/unexpected.json" not in _open(export.build_export(project, "r001")[0]).namelist()
    with pytest.raises(export.ExportError, match="Unknown revision"):
        export.build_export(project, "r002")


def test_an_old_revision_reads_as_unverified_and_unknown(project):
    (Path(project.root) / "r001" / "results.json").write_text('{"summary":{"feasible":true}}', encoding="utf-8")
    manifest = json.loads(_open(export.build_export(project, "r001")[0]).read("export-manifest.json"))
    assert manifest["firmware_validation"]["status"] == "unverified"
    assert set(manifest["stage_status"].values()) == {"unknown"} and manifest["stage_problems"] == {}


def test_full_export_is_the_design_history(project):
    _rev(project, "r002", {"parent": "r001", "summary": {"feasible": True}}, [("cad/robot.glb", b"r2"), ("mfg/a.stl", b"s")])
    bundle = _open(export.build_export(project, None, full=True)[0])
    assert set(bundle.namelist()) == {"export-manifest.json", "meta.json", "r001/design.json", "r001/results.json",
                                      "r002/design.json", "r002/results.json"}
    assert json.loads(bundle.read("meta.json")) == {"kind": "robot", "source": "", "need": "eggs"}  # no stray keys
    manifest = json.loads(bundle.read("export-manifest.json"))
    assert manifest["kind"] == "project" and [r["revision"] for r in manifest["revisions"]] == ["r001", "r002"]
    stream, filename = export.build_export(project, "r002", full=True)
    assert filename == "egg-picker-r002-history.zip"
    names = set(_open(stream).namelist())
    assert {"r002/cad/robot.glb", "r002/mfg/a.stl"} <= names and "r001/cad/robot.glb" not in names


def test_a_real_revision_exports_its_files_and_passes(tmp_path):
    p = RobotProject(str(tmp_path / "egg-picker"))
    rev = p.run(Design().with_changes(EGG_ROBOT), n_trials=1, verbose=False, fast_sim=True)
    bundle = _open(export.build_export(p, rev)[0])
    manifest = json.loads(bundle.read("export-manifest.json"))
    assert manifest["stage_problems"] == {} and manifest["feasible"] is True
    assert set(manifest["stage_status"].values()) == {"computed"} and list(manifest["stage_status"]) == ORDER
    assert manifest["firmware_validation"]["status"] == "unverified"
    names = bundle.namelist()
    for needed in ("cad/robot.glb", "electronics/firmware/morph_arm/morph_arm.ino", "manual/manual.html", "mfg/manufacturing.json"):
        assert needed in names
    assert {n.split("/")[0] for n in names if "/" in n} <= export.ARTIFACT_DIRS


@pytest.fixture
def studio(tmp_path, project):
    st = server.Studio(project, str(tmp_path))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]

    def get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    yield st, get
    httpd.shutdown()
    httpd.server_close()


def test_export_route(studio):
    _, get = studio
    code, headers, body = get("/api/export?rev=r001")
    assert code == 200 and headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"] == 'attachment; filename="egg-picker-r001.zip"'
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert json.loads(zipfile.ZipFile(io.BytesIO(body)).read("export-manifest.json"))["revision"] == "r001"
    assert json.loads(get("/api/export?rev=r001&check=1")[2]) == {"ok": True}
    for path in ("/api/export?rev=../r001", "/api/export?rev=r9", "/api/export", "/api/export?rev=r9&check=1"):
        code, _, body = get(path)
        assert code == 404 and json.loads(body)["ok"] is False
    code, headers, body = get("/api/export?full=1")
    assert code == 200 and "egg-picker-history.zip" in headers["Content-Disposition"]


def test_remote_export_needs_the_access_link(tmp_path, project):
    st = server.Studio(project, str(tmp_path))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None, "test-code-0123456"))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/export?rev=r001")
        res = conn.getresponse()
        assert res.status == 401 and b"PK" not in res.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_linked_design_or_results_never_leave_the_revision(project, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"summary": {"feasible": true}, "label": "sk-ant-not-a-real-key"}', encoding="utf-8")
    folder = Path(project.root) / "r001"
    try:
        (folder / "design.json").unlink()
        os.symlink(secret, folder / "design.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights on this system")
    bundle = _open(export.build_export(project, "r001")[0])
    assert "design.json" not in bundle.namelist()
    (folder / "results.json").unlink()
    os.symlink(secret, folder / "results.json")
    with pytest.raises(export.ExportError) as exc:
        export.build_export(project, "r001")
    assert exc.value.status == 409
