"""Import sessions and the /api/import handlers (contract section 2 request shape, faked); offline, no pipeline runs."""

import io
import json
import os
import time
import types
import zipfile

import pytest

from morph.importers import ImportFailed, session as sess
from morph.studio import api_import
from test_import_fixtures import BOM_MD, arm_urdf, egg_stl


def _upload_dir(s, folder, prefix):
    for dp, _, fns in os.walk(folder):
        for f in fns:
            p = os.path.join(dp, f)
            rel = prefix + "/" + os.path.relpath(p, folder).replace(os.sep, "/")
            with open(p, "rb") as fh:
                s.add_file(rel, fh, os.path.getsize(p))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "projects"
    h.mkdir()
    monkeypatch.setenv("MORPH_HOME", str(h))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    return str(h)


def test_robot_folder_detect_preview_and_commit(home, tmp_path):
    src = tmp_path / "koch-ish"
    arm_urdf(str(src), package="follower")
    (src / "README.md").write_text("Follower arm with XL430-W250 and XL330-M288 servos.", encoding="utf-8")
    s = sess.Session.create(home)
    _upload_dir(s, str(src), "koch-ish")
    events = []
    det = s.detect(events.append)
    c = det["candidates"][0]
    assert c["kind"] == "robot" and c["needs"] == ["actuators"] and not c["ok"]
    assert c["gripper"]["joint"] == "gripper" and c["meshes"] == {"total": 7, "found": 7, "missing": [], "searched": ["follower"]}
    assert c["actuators"]["shoulder_lift"]["options"] == ["xl330_m288", "xl430_w250"]          # both mentioned: Morph asks
    assert "_dir" not in c and {k["key"] for k in det["catalog"]} >= {"sts3215", "xl430_w250", "mg996r"}
    stages = [r["stage"] for r in det["checklist"]]
    assert stages == ["unpack", "meshes", "gripper", "actuators", "tcp"] and det["checklist"][3]["status"] == "warn"
    assert {e["stage"] for e in events} >= {"robot", "meshes", "gripper", "actuators", "detect"}
    assert all(e["type"] == "import" and e["id"] == s.id for e in events)
    png, ctype = s.preview(0, "png")
    assert ctype == "image/png" and png[:8] == b"\x89PNG\r\n\x1a\n"
    glb, ctype = s.preview(0, "glb")
    assert ctype == "model/gltf-binary" and glb[:4] == b"glTF"
    with pytest.raises(ImportFailed, match="Pick the actuators"):
        s.commit(0, "Koch follower", {})
    plan = s.commit(0, "Koch follower", {"family": "dynamixel"})
    assert plan["kind"] == "robot" and plan["source"] == "koch-follower" and plan["project"] == "koch-follower"
    ch = plan["changes"]
    assert ch["arm.source"] == "koch-follower" and ch["arm.actuator_family"] == "dynamixel"
    assert 80 <= ch["task.pick_x_mm"] <= 400 and ch["task.place_y_mm"] > 0 > ch["task.pick_y_mm"]
    assert plan["meta"] == {"kind": "robot", "source": "koch-follower", "origin": "import", "demo": False,
                            "need": plan["meta"]["need"]} and "please confirm" in plan["meta"]["need"]
    folder = os.path.join(home, ".robots", "koch-follower")
    prof = json.load(open(os.path.join(folder, "morph_profile.json"), encoding="utf-8"))
    assert set(prof["actuators"].values()) <= {"xl330_m288", "xl430_w250", "xm430_w350"} and prof["urdf"] == "robot.urdf"
    from morph.robot import imported
    assert imported.load_profile("koch-follower")[1] == os.path.realpath(folder)


def test_zip_upload_and_mixed_candidates(home, tmp_path):
    src = tmp_path / "arm"
    arm_urdf(str(src), motor_names=True)
    (src / "README.md").write_text(BOM_MD, encoding="utf-8")
    z = tmp_path / "arm.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for dp, _, fns in os.walk(src):
            for f in fns:
                zf.write(os.path.join(dp, f), "arm-main/" + os.path.relpath(os.path.join(dp, f), src).replace(os.sep, "/"))
    s = sess.Session.create(home)
    with open(z, "rb") as fh:
        s.add_file("arm.zip", fh, os.path.getsize(z))
    det = s.detect()
    kinds = [c["kind"] for c in det["candidates"]]
    assert kinds == ["robot", "bom"] and det["candidates"][0]["ok"]
    assert det["candidates"][1]["count"] == 5 and det["candidates"][1]["total"] == 116.94
    assert not os.path.exists(os.path.join(s.files, "arm.zip"))                 # unpacked, then removed
    bomres = s.commit(1)
    assert bomres["kind"] == "bom" and len(bomres["rows"]) == 5 and bomres["actuators"] == {"sts3215_7v4": 6}


def test_object_and_nothing_importable(home, tmp_path):
    s = sess.Session.create(home)
    egg = egg_stl(str(tmp_path / "egg.stl"))
    with open(egg, "rb") as fh:
        s.add_file("egg.stl", fh, os.path.getsize(egg))
    det = s.detect()
    c = det["candidates"][0]
    assert det["kind"] == "object" and c["changes"]["object.shape"] == "cylinder" and c["units"]["unit"] == "m"
    assert s.preview(0, "png")[0][:4] == b"\x89PNG"
    res = s.commit(0, None, {"units": "mm"})            # the person says the numbers are millimetres
    assert res["changes"]["object.diameter_mm"] < 1 and res["label"] == "object from egg.stl"
    s2 = sess.Session.create(home)
    s2.add_file("notes.txt", io.BytesIO(b"hello"), 5)
    with pytest.raises(ImportFailed, match="nothing it can import"):
        s2.detect()
    s3 = sess.Session.create(home)
    blob = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64
    s3.add_file("base.SLDPRT", io.BytesIO(blob), len(blob))
    with pytest.raises(ImportFailed, match="native CAD"):
        s3.detect()


def test_broken_robot_is_reported_not_raised(home, tmp_path):
    s = sess.Session.create(home)
    text = b'<robot name="r"><link name="a"/><link name="b"/></robot>'
    s.add_file("r/r.urdf", io.BytesIO(text), len(text))
    det = s.detect()
    c = det["candidates"][0]
    assert c["kind"] == "robot" and not c["ok"] and "root links" in c["error"]
    assert det["checklist"][-1]["status"] == "error"
    with pytest.raises(ImportFailed, match="root links"):
        s.commit(0)


def test_cleanup_removes_old_sessions(home):
    old, new = sess.Session.create(home), sess.Session.create(home)
    past = time.time() - 2 * sess.MAX_AGE_S
    os.utime(os.path.join(old.root, "session.json"), (past, past))
    assert sess.cleanup(home) == 1
    assert not os.path.exists(old.root) and os.path.exists(new.root)
    with pytest.raises(ImportFailed, match="expired"):
        sess.Session.open(old.id, home)


def test_cli_import_dry_run_and_part_refusal(home, tmp_path):
    src = tmp_path / "arm"
    arm_urdf(str(src))
    out = sess.cli_import(str(src), dry_run=True, home_dir=home)
    assert out["dry_run"] and out["kind"] == "robot" and out["candidate"]["needs"] == ["actuators"]
    assert os.listdir(os.path.join(home, ".imports")) == []                    # the session is cleaned up
    assert not os.path.exists(os.path.join(home, ".robots"))
    with pytest.raises(ImportFailed, match="custom printed part"):
        sess.cli_import(str(src), as_="part", home_dir=home)
    with pytest.raises(ImportFailed, match="No board found"):
        sess.cli_import(str(src), as_="board", home_dir=home)


# ------------------------------------------------------------------------------------------- HTTP handlers


class _Hub:
    def __init__(self):
        self.events = []

    def send(self, ev):
        self.events.append(ev)


class _Studio:
    def __init__(self, home):
        self.home, self.hub, self.remote, self.applied = home, _Hub(), False, []
        self.p = types.SimpleNamespace(root=os.path.join(home, "egg-picker"))

    def apply(self, changes, label):
        self.applied.append((changes, label))


def _req(studio, method, path, body=None, query=None, raw=None, length=None):
    headers = {} if raw is None else {"Content-Length": str(len(raw) if length is None else length)}
    handler = types.SimpleNamespace(headers=headers, rfile=io.BytesIO(raw or b""))
    return types.SimpleNamespace(method=method, path=path, body=body, query=query or {}, studio=studio, room=None,
                                 member=None, handler=handler)


def test_api_flow_object(home, tmp_path):
    st = _Studio(home)
    status, out = api_import.dispatch(_req(st, "POST", "/api/import", {}))
    assert status == 200 and out["fetching"] is False
    sid = out["id"]
    egg = open(egg_stl(str(tmp_path / "egg.stl")), "rb").read()
    status, out = api_import.dispatch(_req(st, "PUT", f"/api/import/{sid}/file", query={"path": ["egg.stl"]}, raw=egg))
    assert status == 200 and out["size"] == len(egg) and out["files"] == 1
    status, det = api_import.dispatch(_req(st, "POST", f"/api/import/{sid}/detect", {}))
    assert status == 200 and det["candidates"][0]["kind"] == "object"
    assert any(e["type"] == "import" and e["stage"] == "detect" and e["project"] == "egg-picker" for e in st.hub.events)
    status, data, ctype = api_import.dispatch(_req(st, "GET", f"/api/import/{sid}/preview.png", query={"candidate": ["0"]}))
    assert status == 200 and ctype == "image/png"
    status, out = api_import.dispatch(_req(st, "GET", f"/api/import/{sid}"))
    assert status == 200 and out["detected"]["candidates"][0]["kind"] == "object"
    status, out = api_import.dispatch(_req(st, "POST", f"/api/import/{sid}/commit", {"candidate": 0}))
    assert status == 202 and out["kind"] == "object" and out["applied"]
    for _ in range(50):
        if st.applied:
            break
        time.sleep(0.02)
    assert st.applied[0][0]["object.shape"] == "cylinder" and st.applied[0][1] == "object from egg.stl"
    assert api_import.dispatch(_req(st, "DELETE", f"/api/import/{sid}"))[0] == 200
    assert api_import.dispatch(_req(st, "GET", f"/api/import/{sid}"))[0] == 404


def test_api_errors_are_actionable(home):
    st = _Studio(home)
    sid = api_import.dispatch(_req(st, "POST", "/api/import", {}))[1]["id"]
    s, out = api_import.dispatch(_req(st, "PUT", f"/api/import/{sid}/file", query={"path": ["../../x.stl"]}, raw=b"abc"))
    assert s == 400 and out["ok"] is False and "no '..'" in out["error"]
    req = _req(st, "PUT", f"/api/import/{sid}/file", query={"path": ["a.stl"]}, raw=b"abc")
    req.handler.headers = {}
    assert api_import.dispatch(req)[0] == 411
    s, out = api_import.dispatch(_req(st, "PUT", f"/api/import/{sid}/file", query={"path": ["a.stl"]}, raw=b"x", length=400 << 20))
    assert s == 413
    assert api_import.dispatch(_req(st, "POST", "/api/import/0123456789abcdef/detect", {}))[0] == 404
    assert api_import.dispatch(_req(st, "GET", "/api/import/../../etc/passwd"))[0] == 404
    assert api_import.dispatch(_req(st, "PATCH", f"/api/import/{sid}/detect"))[0] == 405
    s, out = api_import.dispatch(_req(st, "POST", f"/api/import/{sid}/detect", {}))
    assert s == 400 and "Nothing to import yet" in out["error"]
    s, out = api_import.dispatch(_req(st, "POST", "/api/import", {"url": "https://evil.example/robot.zip"}))
    assert s == 400 and "not on the import list" in out["error"]
    st.remote = True
    s, out = api_import.dispatch(_req(st, "POST", "/api/import", {"url": "https://github.com/a/b"}))
    assert s == 400 and "--allow-url-import" in out["error"]
    st.allow_url_import = True
    st.remote = True
    assert api_import._allow_url(st)


def test_api_commit_robot_builds_in_the_background(home, tmp_path, monkeypatch):
    st = _Studio(home)
    sid = api_import.dispatch(_req(st, "POST", "/api/import", {}))[1]["id"]
    src = tmp_path / "arm"
    arm_urdf(str(src), motor_names=True)
    for dp, _, fns in os.walk(src):
        for f in fns:
            p = os.path.join(dp, f)
            rel = "arm/" + os.path.relpath(p, src).replace(os.sep, "/")
            api_import.dispatch(_req(st, "PUT", f"/api/import/{sid}/file", query={"path": [rel]}, raw=open(p, "rb").read()))
    assert api_import.dispatch(_req(st, "POST", f"/api/import/{sid}/detect", {}))[0] == 200
    built = []

    def fake_build(plan, home_dir=None, on_event=None, fast_sim=False, n_trials=2):
        built.append(plan)
        on_event({"type": "node", "node": "sizing", "status": "computed"})
        on_event({"type": "project_created", "project": plan["project"], "origin": "import"})
        return {"project": plan["project"], "rev": "r001", "summary": {}}

    monkeypatch.setattr(api_import.sess, "build", fake_build)
    s, out = api_import.dispatch(_req(st, "POST", f"/api/import/{sid}/commit", {"candidate": 0, "name": "Box Arm"}))
    assert s == 202 and out["kind"] == "robot" and out["project"] == "box-arm" and "design" not in out
    for _ in range(100):
        if any(e.get("stage") == "build" for e in st.hub.events):
            break
        time.sleep(0.02)
    assert built and built[0]["changes"]["arm.source"] == "box-arm"
    node = next(e for e in st.hub.events if e["type"] == "node")
    assert node["project"] == "box-arm"                         # pipeline events are tagged with the new project
    assert any(e["type"] == "project_created" and e["project"] == "box-arm" for e in st.hub.events)
    assert any(e.get("stage") == "build" and e["status"] == "done" for e in st.hub.events)


def test_register_uses_the_router_contract():
    calls = []

    def route(method, path, **kw):
        calls.append((method, path, kw))
        return lambda fn: fn

    api_import.register(route)
    assert ("POST", "/api/import", {"role": "editor"}) in calls
    put = next(c for c in calls if c[0] == "PUT")
    assert put[1] == "/api/import/*" and put[2]["raw_body"] is True and put[2]["max_body"] == 300 << 20


def test_morph_import_cli_helper(home, tmp_path, capsys):
    import argparse

    from morph import importers

    p = argparse.ArgumentParser()
    importers.add_arguments(p)
    src = tmp_path / "arm"
    arm_urdf(str(src))
    assert importers.run_cli(p.parse_args([str(src), "--dry-run"])) == 0
    out = capsys.readouterr().out
    assert "robot: arm/testarm.urdf" in out and "pass --family or --actuators" in out
    out.encode("ascii")                                       # Windows consoles: plain ASCII only
    (tmp_path / "README.md").write_text(BOM_MD, encoding="utf-8")
    assert importers.run_cli(p.parse_args([str(tmp_path / "README.md"), "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["result"]["total"] == 116.94
    assert importers.run_cli(p.parse_args(["https://evil.example/x"])) == 1
    assert "not on the import list" in capsys.readouterr().err


def test_macro_libraries_are_not_robots_and_complete_robots_come_first(home, tmp_path):
    src = tmp_path / "repo"
    arm_urdf(str(src / "urdf" / "good"), name="good")
    arm_urdf(str(src / "urdf" / "partial"), name="partial", missing=("upper_arm", "lower_arm"))
    ctrl = src / "ros2_control"
    ctrl.mkdir(parents=True)
    (ctrl / "arm.ros2_control.xacro").write_text('<robot xmlns:xacro="http://www.ros.org/wiki/xacro">'
                                                 '<xacro:macro name="sys"><ros2_control name="x"/></xacro:macro></robot>', encoding="utf-8")
    s = sess.Session.create(home)
    _upload_dir(s, str(src), "repo")
    files = [c["file"] for c in s.detect()["candidates"]]
    assert files == ["repo/urdf/good/good.urdf", "repo/urdf/partial/partial.urdf"]


def test_mjcf_scene_and_asset_includes(home, tmp_path):
    from test_import_fixtures import stl

    src = tmp_path / "mj"
    stl(str(src / "m.stl"), (0.02, 0.02, 0.02))
    (src / "assets.xml").write_text('<mujoco><asset><mesh name="m" file="m.stl"/></asset></mujoco>', encoding="utf-8")
    (src / "robot.xml").write_text(
        '<mujoco model="r"><compiler angle="radian"/><include file="assets.xml"/><worldbody><body name="base"><geom type="mesh" mesh="m"/>'
        '<body name="l1" pos="0 0 0.05"><joint name="j1" axis="0 0 1" range="-1 1"/><geom type="box" size="0.01 0.01 0.03"/>'
        '</body></body></worldbody></mujoco>', encoding="utf-8")
    (src / "scene.xml").write_text('<mujoco><include file="robot.xml"/><worldbody><geom type="plane" size="1 1 0.1"/>'
                                   '</worldbody></mujoco>', encoding="utf-8")
    s = sess.Session.create(home)
    _upload_dir(s, str(src), "mj")
    cands = [c for c in s.detect()["candidates"] if c["kind"] == "robot"]
    assert [c["file"] for c in cands] == ["mj/robot.xml"]
    assert "arm joints before the gripper" in cands[0]["error"]     # converted fine; one joint is not an arm


def test_kicad_board_candidate_keeps_its_kind(home, tmp_path):
    from test_import_fixtures import KICAD_PCB

    s = sess.Session.create(home)
    data = KICAD_PCB.encode()
    s.add_file("carrier.kicad_pcb", io.BytesIO(data), len(data))
    c = s.detect()["candidates"][0]
    assert c["kind"] == "board" and c["document"] == "pcb" and c["board_mm"] == [50.0, 30.0]
    res = s.commit(0)
    assert res["kind"] == "board" and res["document"] == "pcb" and len(res["rows"]) == 2
