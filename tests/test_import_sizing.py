"""Import as people use it in the Studio (found by driving the Import dialog): picking one actuator family gives a
robot the build calls feasible, a servo picked per joint that would run far below its rated voltage is an issue, an
object mesh brings its own handling, an archive with nothing left says what was skipped, a link that fails at once can
still be asked about, and the build's verdict reaches the importing tab. Offline, no pipeline runs."""

import io
import os
import queue
import threading
import time
import types
import zipfile

import pytest

from morph.importers import ImportFailed, autoprofile, mesh_object, session as sess
from morph.studio import api_import
from test_import_fixtures import egg_stl, so101_copy, stl


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "projects"
    h.mkdir()
    monkeypatch.setenv("MORPH_HOME", str(h))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    return str(h)


def _sized(monkeypatch, need, family, arm=("j1", "j2", "j3"), grip="jaw"):
    from morph.robot import imported
    monkeypatch.setattr(imported, "worst_case_torques", lambda *a, **k: (dict(need), None))
    return autoprofile.size_for_family(None, list(arm), grip, family)


def test_one_family_means_one_servo_rail(monkeypatch):
    # smart bus: the 7.4 V STS3215 everywhere while it holds; one joint past it puts the whole arm on 12 V servos
    assert set(_sized(monkeypatch, {"j2": 0.6}, "smart_bus").values()) == {"sts3215_7v4"}
    assert set(_sized(monkeypatch, {"j2": 1.0, "j3": 0.4}, "smart_bus").values()) == {"sts3215"}
    # dynamixel: XL330s (3.7-6 V) never share a rail with XL430s and XM430s (10-12 V together)
    assert set(_sized(monkeypatch, {"j2": 0.2}, "dynamixel").values()) == {"xl330_m288"}
    got = _sized(monkeypatch, {"j2": 1.0, "j3": 0.3}, "dynamixel")
    assert got["j2"] == "xm430_w350" and got["j3"] == got["j1"] == got["jaw"] == "xl430_w250"
    # hobby PWM servos share a 4.8-6 V rail: the weakest that holds, per joint
    got = _sized(monkeypatch, {"j2": 0.4, "j3": 0.02}, "hobby_pwm")  # j3 at a fifth of j2's load, the jaw at 0.15 N·m
    assert (got["j2"], got["j3"], got["jaw"]) == ("mg996r", "sg90", "mg996r")
    with pytest.raises(ImportFailed, match="Unknown actuator family"):
        _sized(monkeypatch, {}, "stepper")


def test_a_family_pick_is_feasible_in_the_build_sizing(home, tmp_path):
    # the SO-101 from its URDF alone, with "Smart bus" picked: Morph sizes for the worst pose with the motors as bought
    # parts, as the build does, so the shoulder gets a servo that holds it (the 7.4 V one gives 1.91 of 2.02 N·m)
    from morph.robot import imported

    s = sess.Session.create(home)
    s.add_path(so101_copy(str(tmp_path / "so101")))
    s.detect()
    plan = s.commit(0, "so101 bus", {"family": "smart_bus"})
    assert set(plan["actuators"].values()) == {"sts3215"}
    sizing = imported.size(sess.plan_design(plan, "so101-bus"))
    assert sizing["issues"] == [] and all(j["margin"] >= 2.0 for j in sizing["joints"])


@pytest.mark.parametrize("name, size, shape, preset, damage, friction, fsr", [
    ("egg", (44, 44, 57), "cylinder", "raw egg", 25, 0.35, True),        # Morph's preset: the force that cracks it
    ("strawberry", (30, 30, 35), None, "strawberry", 3, 0.6, True),
    ("wooden block", (50, 50, 50), "box", None, 0, 0.5, False),          # robust, wide: the plain finger
    ("bead", (28, 28, 28), None, None, 0, 0.5, True),                    # robust, narrower than the plain finger closes on
])
def test_an_object_mesh_brings_its_own_handling(tmp_path, name, size, shape, preset, damage, friction, fsr):
    p = stl(str(tmp_path / f"{name}.stl"), size) if shape == "box" else str(tmp_path / f"{name}.stl")
    if shape != "box":
        import trimesh
        m = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
        m.apply_scale(tuple(x / 2 for x in size))
        m.export(p)
    res = mesh_object.object_changes(p)
    ch = res["changes"]
    assert res["preset"] == preset and ch["object.max_force_n"] == damage   # never the old object's damage force
    assert ch["object.friction"] == friction
    assert (ch.get("finger.sensor") == "fsr402") == fsr
    if fsr:
        assert (ch["finger.width_mm"], ch["fab.process"]) == (20.0, "fdm_print")


def test_an_archive_with_nothing_left_says_what_it_skipped(home, tmp_path):
    def upload(name, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, (data, link) in entries.items():
                info = zipfile.ZipInfo(n)
                if link:
                    info.create_system, info.external_attr = 3, (0o120777 << 16)
                z.writestr(info, data)
        s = sess.Session.create(home)
        s.add_file(name, io.BytesIO(buf.getvalue()), len(buf.getvalue()))
        return s

    s = upload("links.zip", {"robot/link.urdf": ("/etc/passwd", True)})
    with pytest.raises(ImportFailed, match=r"^Nothing left to import: Morph skipped the symlink robot/link\.urdf\.$"):
        s.detect()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("r.urdf", "<robot/>")
    s = upload("nested.zip", {"inner/robot.zip": (inner.getvalue(), False)})
    with pytest.raises(ImportFailed, match="skipped the nested archive inner/robot.zip: unpack it and drop the folder"):
        s.detect()
    s = sess.Session.create(home)
    with pytest.raises(ImportFailed, match="Nothing to import yet: drop a file or folder"):
        s.detect()


def test_the_importing_tab_hears_whether_the_robot_is_feasible(home, monkeypatch):
    sent, released = [], threading.Event()
    monkeypatch.setattr(api_import, "_release", lambda sid: released.set())
    res = {"summary": {"feasible": False, "issues": ["shoulder_lift: needs 2.02 N·m with margin, sts3215_7v4 gives 1.91 N·m"],
                       "pick_place_success": True},
           "stage_status": {"sizing": "computed", "gripper": "computed", "cad": "reused"}}
    room = types.SimpleNamespace(name="so101-new-calib", hub=types.SimpleNamespace(unsubscribe=lambda q: None),
                                 p=types.SimpleNamespace(load_results=lambda rev: res))
    job = types.SimpleNamespace(result="r001", error=None, status="done", wait=lambda t=None: True)
    s = types.SimpleNamespace(delete=lambda: None)
    api_import._follow("0123456789abcdef", s, room, job, sent.append, queue.Queue())
    assert released.wait(5)
    for _ in range(100):
        if sent:
            break
        time.sleep(0.01)
    done = sent[-1]
    assert (done["stage"], done["status"], done["rev"], done["feasible"]) == ("build", "done", "r001", False)
    assert done["issues"] == res["summary"]["issues"] and done["stages"] == res["stage_status"]
    assert done["pick_place"] is True and done["built_project"] == "so101-new-calib"


def test_an_egg_stl_dropped_on_a_robust_object_arm_gets_the_force_sensing_finger(home, tmp_path):
    s = sess.Session.create(home)
    egg = egg_stl(str(tmp_path / "egg.stl"), metres=False)
    with open(egg, "rb") as fh:
        s.add_file("egg.stl", fh, os.path.getsize(egg))
    c = s.detect()["candidates"][0]
    assert c["preset"] == "raw egg" and c["changes"]["object.max_force_n"] == 25 and c["changes"]["finger.sensor"] == "fsr402"
    plan = s.commit(0)
    assert plan["changes"]["finger.sensor"] == "fsr402" and plan["changes"]["object.friction"] == 0.35


def test_a_servo_far_below_its_rated_voltage_is_said():
    # per-joint picks may put a 12 V STS3215 beside 7.4 V ones: their ranges overlap up to 7.4 V, so the shared rail
    # runs there (12 V through the servo buck: 6 V would leave the 12 V servo half its torque) and the stronger servo
    # gives about 62 % of its rating. Sizing works every margin out at the rail; the plan says why it is low.
    from morph.cli import EGG_ROBOT
    from morph.robot import electronics2, imported
    from morph.spec import Design

    design = Design().with_changes({**EGG_ROBOT, "arm.actuator_family": "smart_bus", "arm.source": "so101"})
    joints = imported.load_profile("so101")[0]["arm_joints"]

    def plan(shoulder):
        return electronics2.plan(design, {"joints": [{"name": n, "actuator": shoulder if n == "shoulder_lift" else "sts3215_7v4"}
                                                     for n in joints], "gripper_actuator": "sts3215_7v4"})
    mixed = plan("sts3215")
    assert mixed["supply"]["servo_v"] == 7.4 and mixed["supply"]["servo_buck"] and mixed["issues"] == []
    assert "shoulder_lift: sts3215 is rated at 12 V but the shared servo rail runs at 7.4 V, so it gives about 62% of its " \
        "rated torque (the torque margins are worked out at 7.4 V); put every servo on one voltage class to use all of it" \
        in mixed["warnings"]
    one = plan("sts3215_7v4")                                       # one voltage class: 6 V keeps 85 % of their torque
    assert one["issues"] == [] and one["supply"]["servo_v"] == 6.0
    assert electronics2._derated([{"joint": "j2", "key": "ds3218"}, {"joint": "j3", "key": "sg90"}], 5.0) == [
        "j2: ds3218 is rated at 6.8 V but the shared servo rail runs at 5 V, so it gives about 74% of its rated torque "
        "(the torque margins are worked out at 5 V)"]


def test_a_link_that_fails_at_once_can_still_be_asked_about(home, monkeypatch):
    # offline, a link fails before the page has the session's id, so its event can't be matched: the session's state
    # says how it went (the Import dialog asks once after the POST)
    from test_import_session import _req, _Studio
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    st = _Studio(home)
    status, out = api_import.dispatch(_req(st, "POST", "/api/import", {"url": "https://github.com/a/b"}))
    assert status == 200 and out["fetching"] is True
    sent = lambda: [e["status"] for e in list(st.hub.events) if e.get("stage") == "fetch"]  # noqa: E731
    for _ in range(500):  # the state is saved just before the event goes out
        got = api_import.dispatch(_req(st, "GET", f"/api/import/{out['id']}"))[1]
        if (got.get("fetch") or {}).get("status") == "error" and "error" in sent():
            break
        time.sleep(0.02)
    assert got["fetch"] == {"status": "error",
                            "error": "Morph is offline (network turned off), so links can't be fetched; drop the files instead."}
    assert sent()[-1] == "error"
