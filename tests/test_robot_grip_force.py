"""One grip force per robot: the gripper stage's force plan (its window, target and hard limit) is what the electronics
plan, the firmware, the simulation and the manual use, for generated arms and imported stock jaws alike."""

import json
import os
import re

import pytest

from morph.cli import EGG_ROBOT
from morph.robot import arm, electronics2, imported, pipeline2, simulate
from morph.spec import Design
from morph.studio.intent import object_changes
from morph.studio.projects import template

STRAWBERRY = object_changes("strawberry")


@pytest.fixture(scope="module", autouse=True)
def _pcb_cache(tmp_path_factory):
    os.environ.setdefault("MORPH_PCB_CACHE", str(tmp_path_factory.mktemp("pcb_cache")))


def _so101(**changes):
    return template("so101", "so101").with_changes({**STRAWBERRY, **changes})


def _firmware_grip(code: str) -> tuple:
    t = float(re.search(r"float GRIP_TARGET_N\s*=\s*([0-9.]+)f;\s*// (.*)", code).group(1))
    lim = float(re.search(r"GRIP_LIMIT_N\s*=\s*([0-9.]+)f", code).group(1))
    return t, lim, re.search(r"float GRIP_TARGET_N .*// (.*)", code).group(1)


def _shipped(d, tmp_path, grip=None):
    """Everything downstream of the gripper stage for design `d`: (force plan, electronics plan, firmware text, the
    target the simulation grips with)."""
    sz = imported.size(d) if d.arm.source else arm.size(d)
    force = grip or pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    ep = electronics2.plan(d, sz, grip=force)
    asm = (imported.build if d.arm.source else arm.build)(d, sz, ep)
    code = (tmp_path / electronics2.firmware(d, ep, asm, str(tmp_path), "r001")).read_text(encoding="utf-8")
    sim_t = simulate._grip_force_target(asm, d, generated_fingers=not d.arm.source)
    return force, ep, code, sim_t


@pytest.mark.parametrize("name, design", [
    ("so101 + strawberry", lambda: _so101()),
    ("so101 + egg", lambda: template("so101", "so101")),
    ("egg-picker", lambda: template("egg-picker")),
    ("soda can arm", lambda: Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none"})),
])
def test_firmware_electronics_and_simulation_ship_the_gripper_stages_grip(tmp_path, name, design):
    d = design()
    force, ep, code, sim_t = _shipped(d, tmp_path)
    lo, hi = force["window_n"]
    t = force["target_force_n"]
    assert lo <= t <= (hi if hi is not None else float("inf")), force
    g = ep["grip"]
    assert (g["window_n"], g["target_n"], g["limit_n"]) == (force["window_n"], t, force["limit_n"])
    fw_t, fw_lim, comment = _firmware_grip(code)
    assert (fw_t, fw_lim) == (t, force["limit_n"])
    assert sim_t == pytest.approx(t)                     # the simulation grips with what the firmware ships
    assert (f"window {lo:g} - {hi:g} N" if hi is not None else f"at least {lo:g} N") in comment
    assert not [i for i in ep["issues"] if "grip" in i], ep["issues"]


def test_the_so101_strawberry_grip_comes_from_the_object_not_the_servo(tmp_path):
    """New robot 'use my SO-101 to pick strawberries' shipped GRIP_TARGET_N = 6.82 (a quarter of the servo's stall
    force) for a 3 N strawberry whose window is 0.25-2.0 N."""
    force, ep, code, _ = _shipped(_so101(), tmp_path)
    # no force sensor on the stock jaws: the open-loop squeeze sits at the window's geometric mean, sqrt(0.25 x 2.0)
    assert force["window_n"] == [0.25, 2.0] and force["target_force_n"] == 0.7 and force["limit_n"] == 2.5
    assert _firmware_grip(code)[0] == 0.7


def test_the_grip_force_parameter_reaches_the_firmware_of_an_imported_robot(tmp_path):
    force, ep, code, sim_t = _shipped(_so101(**{"electronics.target_force_n": 1.0}), tmp_path)
    assert force["target_force_n"] == ep["grip"]["target_n"] == _firmware_grip(code)[0] == sim_t == 1.0


@pytest.mark.parametrize("target, why", [(2.5, "above"), (0.1, "below")])
def test_a_grip_target_outside_its_window_is_an_issue(tmp_path, target, why):
    d = _so101(**{"electronics.target_force_n": target})
    sz = imported.size(d)
    ep = electronics2.plan(d, sz, grip=imported.gripper_summary(d, sz)["force"])
    bad = [i for i in ep["issues"] if i.startswith(f"grip target {target:g} N is {why}")]
    assert len(bad) == 1 and "0.25-2 N" in bad[0], ep["issues"]


def test_a_plan_without_a_gripper_stage_uses_the_same_force_plan():
    """Callers that plan electronics on their own (a quick check, a test) get the gripper stage's closed form, not a
    second formula: the window is the gripper stage's."""
    d = _so101()
    sz = imported.size(d)
    assert electronics2.plan(d, sz)["grip"]["window_n"] == imported.gripper_summary(d, sz)["force"]["window_n"]
    d = template("egg-picker")
    sz = arm.size(d)
    closed = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    assert electronics2.plan(d, sz)["grip"]["window_n"] == closed["window_n"]
    assert electronics2.plan(d, {**sz, "grip_target_n": 2.34})["grip"]["target_n"] == 2.34


def test_the_pipeline_ships_one_grip_from_gripper_stage_to_manual(tmp_path):
    """SO-101 picking strawberries, end to end: gripper stage = electronics plan = firmware = simulation = manual."""
    p = pipeline2.RobotProject(str(tmp_path / "so101-berries"))
    rev = p.run(_so101(name="so101-berries"), n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(rev)
    force, g = res["gripper"]["force"], res["eplan"]["grip"]
    t = force["target_force_n"]
    assert g["target_n"] == res["electronics"]["grip"]["target_n"] == res["summary"]["grip_target_n"] == t
    code = open(os.path.join(p.rev_dir(rev), "electronics", res["electronics"]["firmware"]), encoding="utf-8").read()
    assert _firmware_grip(code)[0] == t
    assert res["sims"]["scenarios"]["pick_place"]["metrics"]["grip_target_n"] == pytest.approx(t, abs=0.01)
    with open(os.path.join(p.rev_dir(rev), "manual", "manual.json"), encoding="utf-8") as fh:
        cal = next(s for s in json.load(fh)["steps"] if s["kind"] == "calibration")
    assert cal["grip_target_n"] == t and cal["grip_window_n"] == force["window_n"]
    text = " ".join(cal["substeps"])
    assert "grip force limit" not in text and f"{t:g} N" in text
    assert res["summary"]["feasible"] is True, res["summary"]["issues"]


def test_the_manual_uses_the_firmwares_commands_and_numbers(tmp_path):
    from morph.robot import manual

    d = template("egg-picker")
    sz = arm.size(d)
    force = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    ep = electronics2.plan(d, sz, grip=force)
    asm = arm.build(d, sz, ep)
    fin = electronics2.finalize(d, sz, ep, asm, str(tmp_path / "r001" / "electronics"))
    m = manual.build(d, asm, None, fin, str(tmp_path / "r001" / "manual"), images=False)
    cal = next(s for s in m["steps"] if s["kind"] == "calibration")
    text = " ".join(cal["substeps"])
    lo, hi = force["window_n"]
    assert f"grips the raw egg at {force['target_force_n']:g} N per finger" in text
    assert f"backs off above {force['limit_n']:g} N" in text and "send F" in text and f"{lo:g}–{hi:g} N" in text
    assert "send Z 200" in text and "fits the curve" not in text and "12.5 N" not in text
    # no force sensors: no sensor calibration, and the manual says what G does instead
    d = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none"})
    sz = arm.size(d)
    ep = electronics2.plan(d, sz)
    asm = arm.build(d, sz, ep)
    fin = electronics2.finalize(d, sz, ep, asm, str(tmp_path / "r002" / "electronics"))
    m = manual.build(d, asm, None, fin, str(tmp_path / "r002" / "manual"), images=False)
    cal = next(s for s in m["steps"] if s["kind"] == "calibration")
    text = " ".join(s for st in m["steps"] for s in st["substeps"])
    assert cal["title"] == "Calibrate joints and gripper" and "no force sensors" in " ".join(cal["substeps"])
    assert "Z 200" not in text and "force-sensor" not in text and "force sensor tare" not in text


# ------------------------------------------------------------------ stock jaws: no force sensors, a torque-limited grip

def _plan_so101(**changes):
    d = _so101(**changes)
    sz = imported.size(d)
    return d, sz, electronics2.plan(d, sz, grip=imported.gripper_summary(d, sz)["force"])


def test_stock_jaws_get_no_force_sensors_bought_wired_or_programmed(tmp_path):
    """The SO-101 bought, wired, programmed and calibrated two FSR 402s that its CAD and manual have no place for."""
    from morph.robot import manual

    assert template("so101", "so101").finger.sensor == "none"
    for sensor in ("none", "fsr402"):                     # a design that still asks for them gets none, and is told so
        d, sz, ep = _plan_so101(**{"finger.sensor": sensor})
        assert ep["grip"]["sensor"] == "none" and not [s for s in ep["sensors"] if s["key"] == "fsr402"]
        warned = imported.gripper_summary(d, sz)["force"]["warnings"]
        assert bool(warned) == (sensor == "fsr402") and all("stock jaws" in w for w in warned)
    asm = imported.build(d, sz, ep)
    fin = electronics2.finalize(d, sz, ep, asm, str(tmp_path / "r001" / "electronics"))
    assert not [r for r in fin["sourcing"] if "FSR" in r["item"]] and not [h for h in fin["harness"] if h["kind"] == "sensor"]
    code = open(tmp_path / "r001" / "electronics" / fin["firmware"], encoding="utf-8").read()
    assert "#define PIN_FSR_L        -1" in code and "#define PIN_FSR_R        -1" in code
    m = manual.build(d, asm, None, fin, str(tmp_path / "r001" / "manual"), images=False)
    text = " ".join(s for st in m["steps"] for s in st["substeps"])
    assert "FSR" not in text and "Z 200" not in text and "calibrate the force sensors" not in text.lower()
    cal = next(s for s in m["steps"] if s["kind"] == "calibration")
    assert "torque limit" in " ".join(cal["substeps"]) and cal["title"] == "Calibrate joints and gripper"
    first = next(s for s in m["steps"] if s["kind"] == "test")["instructions"]    # a strawberry, not a hard-boiled egg
    assert "egg" not in first and "strawberry" in first and "slips" in first


def test_new_robot_with_your_so101_and_imports_keep_the_stock_jaws_sensorless():
    from morph.studio import agents

    b = agents.run_turn(agents.run_turn(text="I have an SO-101 for picking strawberries"), action="defaults")
    assert b.changes["arm.source"] == "so101" and b.changes["finger.sensor"] == "none"
    from morph.studio import intent
    r = intent.interpret("add a force sensor", template("so101", "so101"))
    assert r["changes"] == {} and "stock jaws" in r["reply"]


def test_the_gripper_torque_limit_comes_from_the_grip():
    d, sz, ep = _plan_so101()
    tl = ep["grip"]["torque_limit"]
    stall = 1.62                                   # sts3215_7v4 on the SO-101's 6 V rail
    lever = ep["grip"]["lever_mm"]                 # where the simulation grasps the strawberry, from the jaw's pivot
    assert lever == pytest.approx(70.0, abs=1.0)
    assert tl["register"] == "feetech_torque_limit" and tl["force_n"] == ep["grip"]["target_n"] == 0.7
    assert tl["nm"] == pytest.approx(0.7 * lever / 1000, abs=1e-4)
    assert tl["fraction"] == pytest.approx(0.7 * lever / 1000 / stall, abs=1e-4)
    egg = template("egg-picker")                  # force sensors: a hard cap at the firmware's limit, no gear losses
    sz = arm.size(egg)
    g = electronics2.plan(egg, sz, grip=pipeline2.gripper_stage(egg, sz, simulate=False)["force"])["grip"]
    assert g["torque_limit"]["force_n"] == g["limit_n"] == 19.0
    assert g["torque_limit"]["nm"] == pytest.approx(19.0 * 2 * egg.electronics.pinion_radius_mm / 1000, abs=1e-4)
    assert [w for w in ep["warnings"] if "G closes the jaws past the object and the gripper servo's torque limit sets "
            "the squeeze (3 % of its stall torque for 0.7 N" in w]
    pwm = _plan_so101(**{"arm.actuator_family": "hobby_pwm"})
    assert pwm[2]["grip"]["torque_limit"] is None
    assert [w for w in pwm[2]["warnings"] if "G closes the jaws only" in w and "position loop squeezes" in w]
    assert any("no torque limit" in i for i in imported.gripper_summary(pwm[0], pwm[1])["force"]["issues"])
    # on the stock jaw's 70 mm lever one PCA9685 count is 0.54 mm at the strawberry: no hobby close is gentle enough
    assert [i for i in pwm[2]["issues"] if "the least it can close past the strawberry" in i]


def test_new_robot_and_teams_estimate_the_stock_jaws_lever_without_searching(tmp_path, monkeypatch):
    """New robot's preflight and Teams' change-request preview ran the simulation's seat search for every new object
    size on an imported robot, inside the request: 28 s for a soda can on the SO-101, 3-9 s for each egg size. They now
    take the lever the build worked out for that object (or for one of its shape within 2 mm of its size), else the
    jaw's mid-jaw lever (70 mm). The search itself puts the SO-101's objects 20-66 mm across 62-80 mm from the pivot,
    so the estimate's stall force is within 15 % of the build's; the grip target comes from the object's window and
    is the build's."""
    from morph.robot.pipeline2 import ORDER, RobotProject
    from morph.studio import brief
    from morph.studio.teams import impact

    monkeypatch.setattr(imported, "_LEVERS", {})     # only the levers this test's builds work out (not other tests')
    egg = template("so101", "so101")
    sz = imported.size(egg)
    builds = {o: imported.gripper_summary(egg.with_changes(object_changes(o)), sz)["force"] for o in ("raw egg", "strawberry")}
    monkeypatch.setattr(simulate, "grasp_lever", lambda *a, **k: pytest.fail("the seat search ran inside a request"))
    for o, built in builds.items():                  # the build worked these out: the preflight takes them as they are
        f = brief.preflight(egg.with_changes(object_changes(o)).flat())["grip"]
        assert (f["lever_mm"], f["target_force_n"], f["limit_n"]) == (built["lever_mm"], built["target_force_n"],
                                                                      built["limit_n"]) and "lever_estimated" not in f
    near = brief.preflight(egg.with_changes({"object.diameter_mm": 43.0}).flat())["grip"]   # a 1 mm smaller egg
    assert near["lever_mm"] == builds["raw egg"]["lever_mm"] and near["lever_estimated"]
    can = brief.preflight(egg.with_changes(object_changes("soda can")).flat())
    f = can["grip"]
    assert f["lever_mm"] == imported.JAW_LEVER_MM and f["lever_estimated"] and can["ms"] < 20000
    hold = 0.37 * 9.81 / (2 * 0.35)                   # the can's window: 3 x its static hold, whatever the lever
    assert f["target_force_n"] == pytest.approx(3 * hold, abs=0.01)
    for built in builds.values():
        assert abs(imported.JAW_LEVER_MM / built["lever_mm"] - 1) < 0.15
    # Teams' preview of an object change on a built SO-101 goes through the same preflight
    root = tmp_path / "so101"
    (root / "r001").mkdir(parents=True)
    ep = electronics2.plan(egg, sz, grip=builds["raw egg"])
    res = {"rev": "r001", "kind": "robot", "sizing": sz, "eplan": ep, "gripper": {"force": builds["raw egg"]},
           "provenance": {s: "computed" for s in ORDER}, "summary": {"unit_cost": 300.0, "issues": []}}
    for k in [k for s in ORDER for k in RobotProject._keys(s)]:
        res.setdefault(k, {"computed_in": "r001"})
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(json.dumps(egg.to_dict()), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps(res), encoding="utf-8")
    out = impact.impact(str(root), "r001", object_changes("soda can"))
    grip = next(r for r in out["rows"] if r["key"] == "grip")
    assert not out.get("error") and grip["text"] == f"grip force target 4.58 → {f['target_force_n']:g} N"


@pytest.mark.parametrize("obj", ["strawberry", "raw egg"])
def test_the_simulation_squeezes_a_stock_jaw_with_the_firmwares_torque_limit(obj):
    """The torque limit was set for the target 70 mm from the SO-101's pivot, but the simulation grasps the egg 62.2 mm
    from it: it squeezed 2.83 N (3.41 N peak) for a 2.52 N target. The limit is now set at the lever where the grasp
    is, so the squeeze at the object is the target."""
    d = template("so101", "so101") if obj == "raw egg" else _so101()
    sz = imported.size(d)
    ep = electronics2.plan(d, sz, grip=imported.gripper_summary(d, sz)["force"])
    asm = imported.build(d, sz, ep)
    assert asm.meta["grip_torque_nm"] == ep["grip"]["torque_limit"]["nm"]
    info, _ = simulate._describe(asm, d, simulate._opts(None))
    assert ep["grip"]["lever_mm"] == pytest.approx(info.lever_mm, abs=0.01)
    assert info.grip_effort == pytest.approx(ep["grip"]["torque_limit"]["nm"])
    assert info.grip_effort / (info.lever_mm / 1000) == pytest.approx(ep["grip"]["target_n"], rel=2e-3)
    assert info.grip_force_n == pytest.approx(ep["grip"]["target_n"])


HOBBY_CAN = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none",
                                   "arm.actuator_family": "hobby_pwm"})


def test_a_sensorless_hobby_gripper_closes_only_as_far_as_its_squeeze_and_is_simulated_so():
    """A hobby PWM servo has no torque limit and no overload protection. Its firmware commanded GRIP_MIN, 3 mm past the
    can, so the MG996R sat at stall (30.9 N per finger, its whole stall current) for every carry, while the power budget
    counted the 13.94 N target. G now closes only as far past the object as the servo's own position loop needs for the
    target (it reaches stall SERVO_BAND_MM past contact), never past what the servo holds continuously (its rated
    torque: the can's 13.94 N target becomes 10 N, still above the 6.97 N it needs not to slip), and the budget, the
    simulation, the plan and the manual use that squeeze. A can at the large end of its size still meets the stall, so
    the plan has an issue (test_a_sensorless_hobby_grip_keeps_every_size_of_its_object_under_stall_or_says_it_cant)."""
    from morph import electronics
    from morph.robot import catalog

    d = HOBBY_CAN
    sz = arm.size(d)
    ep = electronics2.plan(d, sz, grip=pipeline2.gripper_stage(d, sz, simulate=False)["force"])
    g, ol = ep["grip"], ep["grip"]["open_loop"]
    s = catalog.ACTUATORS["mg996r"]
    rated = g["stall_force_n"] * s["rated_nm"] / s["torque_nm"]
    assert g["torque_limit"] is None and g["sensor"] == "none" and g["stall_force_n"] > g["target_n"] > rated
    assert ol["rated_n"] == pytest.approx(rated, abs=0.005) and ol["capped"]
    assert ol["squeeze_mm"] == pytest.approx(electronics.SERVO_BAND_MM * rated / g["stall_force_n"], abs=1e-4)
    assert g["squeeze_n"] == ol["rated_n"] < g["target_n"] and g["squeeze_n"] > g["window_n"][0]
    assert [i[:91] for i in ep["issues"]] == ["gripper: the hobby gripper servo has no torque limit and closes by "
                                             "position only, so a soda"]
    # the power budget counts the servo's share of stall at that squeeze, not a stall
    row = next(a for a in ep["power_budget"]["per_actuator"] if a["joint"] == "gripper")
    share = electronics.jaw_torque(g["squeeze_n"], g["lever_mm"], g["jaw"]) / s["torque_nm"]
    assert row["avg_a"] == pytest.approx(s["idle_a"] + s["stall_a"] * share * 0.5, abs=0.01)
    assert row["avg_a"] < s["stall_a"] / 2
    w = next(w for w in ep["warnings"] if f"G closes the jaws only {ol['squeeze_mm']:.2f} mm past the calibration gauge" in w)
    assert (f"less than the {g['target_n']:g} N target: the most it holds continuously, its rated torque), 32 % of its "
            f"stall force and current. A soda can at the large end of its size, 2.64 mm per jaw wider than the gauge, "
            f"gets its whole stall force (a plan issue).") in w and "100 %" not in w
    assert w.endswith(f"F <newtons> sets how far G closes, up to {g['limit_n']:g} N (the servo's rated torque)")
    assert not [w for w in ep["warnings"] if "rated to hold continuously" in w]
    assert g["limit_n"] == ol["rated_n"]          # F goes up to the rated torque, never past it
    # the simulation holds the jaw the same distance past contact, with the servo's stall force as its limit
    asm = arm.build(d, sz, ep)
    assert (asm.meta["grip_squeeze_mm"], asm.meta["grip_stall_n"]) == (ol["squeeze_mm"], g["stall_force_n"])
    info, _ = simulate._describe(asm, d, simulate._opts(None))
    assert info.grip_open_loop and info.grip_contact - info.grip_close == pytest.approx(ol["squeeze_mm"], abs=1e-6)
    assert info.grip_force_n == g["squeeze_n"]
    assert info.grip_effort == pytest.approx(g["stall_force_n"] * len(info.jaw_joints))
    m = simulate.run(asm, d, "shake", fast=True)["metrics"]
    assert m["success"] and m["slip_mm"] < 5.0 and m["grip_n_max"] < 0.6 * g["stall_force_n"], m


def test_a_hobby_grip_the_servo_cant_hold_continuously_is_an_issue_not_a_stall():
    """A 900 g can on the MG996R: its 33.9 N target is above the servo's 30.9 N stall, so G closed it SERVO_BAND_MM past
    contact and held it at stall for the whole carry, and the warning said "100 % of its stall force ... (it would reach
    its stall ... and sit there)". It now squeezes the 10 N the servo holds continuously, and the plan says that is
    below the 16.95 N the can needs not to slip."""
    d = HOBBY_CAN.with_changes({"object.mass_g": 900.0})
    sz = arm.size(d)
    ep = electronics2.plan(d, sz, grip=pipeline2.gripper_stage(d, sz, simulate=False)["force"])
    g, ol = ep["grip"], ep["grip"]["open_loop"]
    assert g["target_n"] > g["stall_force_n"] and ol["capped"] and g["squeeze_n"] == g["limit_n"] == ol["rated_n"]
    assert ol["squeeze_mm"] < 0.4 * ol["servo_band_mm"]
    assert ep["issues"][1:] == [f"gripper: the hobby gripper servo has no torque limit and holds only {ol['rated_n']:g} N "
                                f"per finger continuously (its rated torque), below the {g['window_n'][0]:g} N the soda can "
                                "needs not to slip: use a stronger gripper servo, or a bus servo whose torque limit sets "
                                "the squeeze"]
    assert "closes by position only" in ep["issues"][0]          # and a can at the large end of its size stalls it
    assert not [w for w in ep["warnings"] if "100 %" in w]


def _compile_firmware(tmp_path, d):
    import shutil
    import subprocess

    if shutil.which("g++") is None:
        pytest.skip("g++ not installed")
    sz = imported.size(d) if d.arm.source else arm.size(d)
    ep = electronics2.plan(d, sz, grip=pipeline2.gripper_stage(d, sz, simulate=False)["force"])
    asm = (imported.build if d.arm.source else arm.build)(d, sz, ep)
    src = tmp_path / electronics2.firmware(d, ep, asm, str(tmp_path), "r001")
    stubs = os.path.join(os.path.dirname(__file__), "fw_stubs")
    with open(os.path.join(stubs, "grip_harness.cpp"), encoding="utf-8") as fh:
        (tmp_path / "run.cpp").write_text(src.read_text(encoding="utf-8") + "\n" + fh.read(), encoding="utf-8")
    exe = tmp_path / "run.exe"
    res = subprocess.run(["g++", "-std=gnu++17", "-I" + stubs, str(tmp_path / "run.cpp"), "-o", str(exe)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return exe, ep


def _run_exe(exe, newtons_per_unit, offset=0.0, f=None):
    """Run the compiled sketch's grip loop: a force sensor reading `newtons_per_unit` per unit closed past the object,
    which meets the jaws `offset` units past the calibrated zero (an object narrower than the gauge); `f`: an F command
    (newtons) sent before G."""
    import subprocess

    args = [str(exe), str(newtons_per_unit), str(offset)] + ([str(f)] if f is not None else [])
    out = subprocess.run(args, capture_output=True, text=True, timeout=120).stdout
    fields = dict(kv.split("=") for kv in next(ln for ln in out.splitlines() if ln.startswith("RESULT")).split()[1:])
    return {k: (float(v) if k not in ("state",) else v) for k, v in fields.items()}, out


def _run_firmware(tmp_path, d, newtons_per_unit, offset=0.0):
    exe, ep = _compile_firmware(tmp_path, d)
    return (*_run_exe(exe, newtons_per_unit, offset), ep)


def test_the_firmware_with_no_force_reading_stops_under_its_torque_cap_and_says_so(tmp_path):
    """The generated firmware itself, compiled and run: with no force reading all the way to its max-squeeze stop it
    goes back to GRIP_CLOSED and says so (meanwhile the torque limit, written at start-up, caps the squeeze at the
    firmware's force limit); a working sensor still closes to the target."""
    r, out, ep = _run_firmware(tmp_path / "egg0", template("egg-picker"), 0.0)
    assert r["lowest"] == pytest.approx(r["min"], abs=0.1)           # probed to the stop (within one 10 ms tick)
    assert r["final"] == r["closed"] and r["state"] == "POS"
    assert "!GRIP no force reading down to GRIP_MIN" in out and r["torque_writes"] == 1
    tl = ep["grip"]["torque_limit"]              # the cap: the force limit at the pinion with no gear losses
    assert tl["force_n"] == ep["grip"]["limit_n"] and r["torque"] == round(tl["fraction"] * 1000)
    r, out, ep = _run_firmware(tmp_path / "egg20", template("egg-picker"), 20.0)   # 20 N per mm past contact
    assert r["state"] == "HOLD" and r["closed"] > r["final"] > r["min"] and "!GRIP" not in out
    assert abs(r["force"] - ep["grip"]["target_n"]) <= 0.15 * ep["grip"]["target_n"]


def test_a_force_sensing_gripper_grips_an_object_narrower_than_its_gauge(tmp_path):
    """The robustness study varies the egg from 92 % to 108 % of its size; the jaws are calibrated on a gauge of the
    nominal size. The force-sensing close used to give up 1 mm past the gauge ('no force reading') and release an egg
    more than 2 mm narrower; it now closes as far as GRIP_MIN looking for the target, on soft and stiff objects."""
    d = template("egg-picker")
    exe, ep = _compile_firmware(tmp_path, d)
    t = ep["grip"]["target_n"]
    band = 0.08 * d.object.diameter_mm / 2                     # 92 % of the size: 1.76 mm per jaw for a 44 mm egg
    for k in (20.0, 5.0):
        for off in (0.0, 1.2, band, 2.5):
            r, out = _run_exe(exe, k, off)
            assert r["state"] == "HOLD" and "!GRIP" not in out, (k, off, r)
            assert abs(r["force"] - t) <= 0.15 * t and r["min"] < r["final"] < -off, (k, off, r)


def test_sensorless_jaws_close_past_the_object_so_the_torque_limit_sets_the_squeeze(tmp_path):
    """A sensorless rack-and-pinion gripper closed to the calibrated contact (gripper = 0), where the jaws only touch a
    gauge-size object: no position error, so the torque limit the manual and the plan promised never squeezed. G now
    closes towards GRIP_MIN, past the object, with the torque limit written; the simulation commands the same."""
    can = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none"})
    for name, d in (("can", can), ("so101", _so101())):
        r, out, ep = _run_firmware(tmp_path / name, d, 0.0)
        tl = ep["grip"]["torque_limit"]
        assert r["lowest"] == r["final"] == r["min"] < r["closed"] and r["state"] == "POS" and "!GRIP" not in out, r
        assert r["torque_writes"] == 1 and r["torque"] == round(max(tl["fraction"] * 1000, 10)) and tl["force_n"] == ep["grip"]["target_n"]
        code = open(next((tmp_path / name).rglob("morph_arm.ino")), encoding="utf-8").read()
        assert "close past the object towards GRIP_MIN; the gripper servo's torque limit sets the squeeze" in code
        sz = imported.size(d) if d.arm.source else arm.size(d)
        asm = (imported.build if d.arm.source else arm.build)(d, sz, ep)
        info, _ = simulate._describe(asm, d, simulate._opts(None))
        assert info.grip_close < info.grip_contact                      # the simulated jaws are commanded past it too
        assert info.grip_force_n == pytest.approx(ep["grip"]["target_n"])   # and squeeze at the torque limit's force


@pytest.mark.parametrize("name, design", [
    ("can, no force sensors", lambda: HOBBY_CAN),
    # a target above the servo's stall: G held it SERVO_BAND_MM past contact, at stall, for the whole carry
    ("900 g can, no force sensors", lambda: HOBBY_CAN.with_changes({"object.mass_g": 900.0})),
    ("1100 g can, no force sensors", lambda: HOBBY_CAN.with_changes({"object.mass_g": 1100.0})),
    ("1400 g can, no force sensors", lambda: HOBBY_CAN.with_changes({"object.mass_g": 1400.0})),
    ("egg, no force sensors", lambda: template("egg-picker").with_changes({"arm.actuator_family": "hobby_pwm",
                                                                         "finger.sensor": "none"})),
    ("egg, force sensors", lambda: template("egg-picker").with_changes({"arm.actuator_family": "hobby_pwm"})),
    ("strawberry, force sensors", lambda: template("egg-picker").with_changes({**STRAWBERRY,
                                                                               "arm.actuator_family": "hobby_pwm"})),
])
def test_no_hobby_servo_is_commanded_to_stall(tmp_path, name, design):
    """The compiled firmware of a hobby PWM gripper (no torque limit, no overload protection): its deepest command past
    the gauge contact stays short of SERVO_BAND_MM, where its position loop would reach stall. Without force sensors G
    holds GRIP_SQUEEZE past contact, never past the servo's rated torque, also when the target is above its stall or F
    asks for far more, and F raises a squeeze below that up to it; with them a close that reads nothing stops at
    GRIP_PROBE (where a gauge-sized object gets about the hard limit) and opens again, saying so; a close that reads
    the force stops at the target."""
    from morph import electronics

    d = design()
    exe, ep = _compile_firmware(tmp_path, d)
    g, ol, band = ep["grip"], ep["grip"]["open_loop"], electronics.SERVO_BAND_MM
    fsr = g["sensor"] == "fsr402"
    raise_to = ol.get("rated_n")                            # F up to the servo's rated torque or the object's limit
    runs = [(0.0, 0.0, None), (20.0, 0.0, None)] + ([] if fsr else [(0.0, 0.0, 1000.0), (0.0, 0.0, 5.0),
                                                                     (0.0, 0.0, raise_to)])
    if not fsr:
        assert g["limit_n"] == min(g["limit_n"], ol["rated_n"]) and g["squeeze_n"] <= ol["rated_n"]
    for k, off, f in runs:
        r, out = _run_exe(exe, k, off, f)
        deepest = r["closed"] - r["lowest"]                 # mm past the gauge contact (a rack-and-pinion jaw)
        assert deepest < band, (name, k, f, r)
        if not fsr:
            want = min(f or g["squeeze_n"], g["limit_n"]) * band / g["stall_force_n"]
            assert r["final"] == r["lowest"] and deepest == pytest.approx(max(want, 2 * ol["step_mm"]), abs=1e-3), r
            assert r["zero"] - r["pulse"] >= 2 * electronics2.PWM_STEP_US - 1, r       # the servo is sent past contact
            assert g["stall_force_n"] * deepest / band <= g["limit_n"] + 0.01 and "!GRIP" not in out
            assert deepest <= band * ol["rated_n"] / g["stall_force_n"] + 1e-3, r      # never past its rated torque
        elif k == 0.0:                                      # no force reading: stops at the probe, opens, says so
            assert deepest == pytest.approx(ol["probe_mm"], abs=1e-3) and r["final"] == r["max"], r
            assert r["state"] == "POS" and "!GRIP no force reading down to GRIP_PROBE past GRIP_CLOSED: opened to GRIP_MAX" in out
            assert ol["probe_mm"] <= electronics.PROBE_STALL_SHARE * band + 1e-9            # a fifth short of stall
            assert ol["probe_n"] == pytest.approx(g["stall_force_n"] * ol["probe_mm"] / band, abs=0.01)
        else:
            assert r["state"] == "HOLD" and abs(r["force"] - g["target_n"]) <= 0.15 * g["target_n"], r


@pytest.mark.parametrize("obj, changes, covers", [
    ("marker", {}, True), ("marker", {"object.max_force_n": 20.0}, True),    # 18 mm: 0.72 mm per jaw over at 108 %
    ("soda can", {}, False), ("water bottle", {}, False), ("soda can", {"object.mass_g": 900.0}, False)])
def test_a_sensorless_hobby_grip_keeps_every_size_of_its_object_under_stall_or_says_it_cant(tmp_path, obj, changes, covers):
    """A hobby PWM gripper without force sensors closes by position only, and G set how far past the calibration gauge
    for a gauge-sized object: one larger than the gauge within its size band (OBJECT_SIZE_SPREAD, 8 % each way) is held
    that much further past its own contact. A 370 g can 2 % over its nominal 66 mm got 76 % of the MG996R's stall, from
    4 % over its whole stall, for the whole carry and with no overload protection, and the simulation (and so a
    robustness study) counted it placed; a sensorless egg 6 % over was crushed. G now closes no further than keeps an
    object at the large end of the band under PROBE_STALL_SHARE of the stall and under its damage force, F can't raise
    it past that, and when no close can (the can is 2.64 mm per jaw over at 108 %, past the 1.2 mm to that share) the
    plan says so as an issue New robot repairs; the simulation fails a trial that holds the servo at stall."""
    from morph import electronics
    from morph.studio import brief

    d = Design().with_changes({**EGG_ROBOT, **object_changes(obj), "finger.sensor": "none",
                               "arm.actuator_family": "hobby_pwm", **changes})
    exe, ep = _compile_firmware(tmp_path, d)
    g, ol, band = ep["grip"], ep["grip"]["open_loop"], electronics.SERVO_BAND_MM
    spread = electronics.OBJECT_SIZE_SPREAD * d.object.diameter_mm / 2
    dmg = d.object.max_force_n
    room = min(electronics.PROBE_STALL_SHARE * band, band * dmg / g["stall_force_n"] if dmg else band)
    assert ol["spread_mm"] == pytest.approx(spread, abs=1e-3) and ol["covers"] is covers
    told = [i for i in ep["issues"] if "closes by position only" in i]
    assert bool(told) is not covers and brief.classify(told) == ({"hobby_gripper"} if told else set()), ep["issues"]
    if told:
        assert f"{spread:.2f} mm per jaw wider than the calibration gauge" in told[0], told
    for f in (None, 1000.0, 3.0):
        r, out = _run_exe(exe, 0.0, 0.0, f)
        deepest = r["closed"] - r["lowest"]                     # mm past the gauge contact: the most G or F commands
        if covers:   # the largest object of the band stays under 80 % of stall and under its damage force
            assert deepest + spread <= room + 1e-3 and deepest >= 2 * ol["step_mm"] - 1e-3, (f, r)
            if f is None:
                assert deepest == pytest.approx(ol["squeeze_mm"], abs=1e-3), r
                assert ol["large_n"] == pytest.approx(g["stall_force_n"] * (deepest + spread) / band, abs=0.02)
        else:        # the plan's issue; G still squeezes a gauge-sized object as planned
            want = min(f or g["squeeze_n"], g["limit_n"]) * band / g["stall_force_n"]
            assert deepest == pytest.approx(max(want, 2 * ol["step_mm"]), abs=1e-3), (f, r)
    if covers:
        assert ol["large_n"] <= electronics.PROBE_STALL_SHARE * g["stall_force_n"] + 0.01
        assert not dmg or ol["large_n"] <= dmg
        w = next(w for w in ep["warnings"] if "G closes the jaws only" in w)
        assert (f"at the large end of its size, {spread:.2f} mm per jaw wider than the gauge, gets about "
                f"{ol['large_n']:g} N") in w
    sz = arm.size(d)
    asm = arm.build(d, sz, ep)
    m = simulate.run(asm, d, "pick_place", perturb={"size_scale": 1.0 + electronics.OBJECT_SIZE_SPREAD},
                     fast=True)["metrics"]
    if covers:       # (an 18 mm marker misses its place on this arm whatever the gripper: not asserted here)
        assert "grip_stalled" not in m, m
    else:
        assert not m["success"] and m["grip_stalled"]["wider_mm"] == pytest.approx(spread, abs=1e-3), m
        assert "held at its stall" in m["failure"], m["failure"]
    m = simulate.run(asm, d, "pick_place", fast=True)["metrics"]           # a gauge-sized object never stalls it
    assert "grip_stalled" not in m, m


@pytest.mark.parametrize("obj, covers", [("egg", False), ("grape", False), ("marker", True)])
def test_a_hobby_force_sensing_close_reaches_the_small_end_of_the_size_or_says_it_cant(tmp_path, obj, covers):
    """A hobby PWM gripper with force sensors gave up any close that read nothing GRIP_PROBE past the gauge: 0.92 mm for
    an egg (an egg 1.76 mm per jaw narrower, the robustness study's 92 %, was let go) and two PCA9685 counts for a
    strawberry, while the simulation and the robustness trials gripped them, and the build read feasible. The probe now
    reaches the small end of the object's size (OBJECT_SIZE_SPREAD) when a dead sensor then leaves a gauge-sized object
    under PROBE_STALL_SHARE of the stall and its damage force; when it can't (the egg would need 1.92 mm, past the
    stall; a dead sensor at the grape's 0.96 mm would crush it) the plan says so as an issue. The simulation lets go
    exactly the sizes the compiled firmware lets go."""
    from morph import electronics

    d = template("egg-picker").with_changes({**({} if obj == "egg" else object_changes(obj)),
                                             "arm.actuator_family": "hobby_pwm"})
    exe, ep = _compile_firmware(tmp_path, d)
    ol = ep["grip"]["open_loop"]
    R = d.object.diameter_mm / 2
    spread = electronics.OBJECT_SIZE_SPREAD * R
    assert ol["spread_mm"] == pytest.approx(spread, abs=1e-3) and ol["covers"] is covers
    assert ol["probe_mm"] <= electronics.PROBE_STALL_SHARE * electronics.SERVO_BAND_MM + 1e-9
    assert (ol["probe_mm"] > spread) is covers
    told = [i for i in ep["issues"] if "no torque limit to back up the force sensors" in i]
    assert bool(told) is not covers and (not told or "more than that narrower per jaw isn't gripped" in told[0])
    sz = arm.size(d)
    asm = arm.build(d, sz, ep)
    assert asm.meta["grip_probe_mm"] == ol["probe_mm"]
    for s in (0.98, 0.95, 0.92):
        narrower = R * (1 - s)
        r, out = _run_exe(exe, 20.0, narrower)
        gripped = r["state"] == "HOLD" and "!GRIP" not in out
        assert gripped is (narrower <= ol["probe_mm"]) and (gripped or covers is False), (s, r)
        m = simulate.run(asm, d, "pick_place", perturb={"size_scale": s}, fast=True)["metrics"]
        assert ("grip_gave_up" not in m) is gripped, (s, m.get("grip_gave_up"))
        if not gripped:
            assert not m["success"] and "opens with !GRIP no force reading" in m["failure"], m


def test_a_hobby_force_sensing_probe_is_no_reach_a_dead_sensor_would_crush_the_object_with(tmp_path):
    """The hobby force-sensing probe reached a 20 mm grape at the small end of its size (0.96 mm past the gauge), so a
    dead sensor put 19.77 N on a gauge-sized grape, ten times the 2 N that damages it, before the jaw opened, and the
    plan called the grape covered. Reaching it is now an issue that says so, and the probe stays where a gauge-sized
    grape gets the least the servo can be sent (two position commands, 3.31 N), as for the egg and the strawberry."""
    from morph import electronics

    d = template("egg-picker").with_changes({**object_changes("grape"), "arm.actuator_family": "hobby_pwm"})
    exe, ep = _compile_firmware(tmp_path, d)
    g, ol, band = ep["grip"], ep["grip"]["open_loop"], electronics.SERVO_BAND_MM
    assert ol["reach_mm"] == pytest.approx(0.961, abs=1e-3) and not ol["covers"]
    assert ol["probe_mm"] == pytest.approx(2 * ol["step_mm"], abs=1e-3) and ol["probe_n"] == pytest.approx(3.31, abs=0.01)
    told = next(i for i in ep["issues"] if "no torque limit to back up the force sensors" in i)
    assert ("where a dead sensor would put 19.77 N per finger, 64 % of its stall force, on a gauge-sized grape, above "
            "the 2 N that damages it: the firmware gives up at 0.16 mm") in told, told
    w = next(w for w in ep["warnings"] if "stops there and opens (!GRIP)" in w)
    assert "a gauge-sized grape gets about 3.31 N per finger there" in w and "(above the 2 N that damages it)" in w
    r, out = _run_exe(exe, 0.0, 0.0)                       # a dead sensor: the close stops at the probe and opens
    assert r["closed"] - r["lowest"] == pytest.approx(ol["probe_mm"], abs=1e-3) and r["final"] == r["max"], r
    assert g["stall_force_n"] * (r["closed"] - r["lowest"]) / band < 0.2 * g["stall_force_n"]


@pytest.mark.parametrize("width, dmg", [(22.0, 25.0), (24.0, 30.0), (22.0, 0.0), (25.0, 0.0)])
def test_a_dead_sensor_never_crushes_the_large_end_of_a_covered_hobby_grips_size(tmp_path, width, dmg):
    """A hobby force-sensing gripper's probe was checked with a dead sensor on a gauge-sized object only. A 22 mm flat
    part with a 25 N damage force read covered with no issue, while with a dead sensor the compiled firmware closed one
    2 % over its size 1.26 mm past its contact (25.94 N) and one 4 % over to the MG996R's stall (30.86 N) before
    opening. Covering the small end now also needs a dead sensor to leave the large end of the size under the damage
    force; when it can't, the plan says so (an issue New robot repairs). An object with no damage force can briefly
    reach the stall before the jaw opens, and the plan's warning says what the large end gets."""
    from morph import electronics
    from morph.studio import brief

    d = template("egg-picker").with_changes({
        **object_changes("marker"), "object.name": "tile", "object.shape": "box", "object.diameter_mm": width,
        "object.length_mm": width, "object.mass_g": 30.0, "object.max_force_n": dmg, "object.friction": 0.5,
        "finger.sensor": "fsr402", "arm.actuator_family": "hobby_pwm"})
    exe, ep = _compile_firmware(tmp_path, d)
    g, ol, band = ep["grip"], ep["grip"]["open_loop"], electronics.SERVO_BAND_MM
    spread = electronics.OBJECT_SIZE_SPREAD * width / 2
    worst = 0.0
    for s in (1.0, 1.02, 1.04, 1.06, 1.08):
        r, out = _run_exe(exe, 0.0, width / 2 * (1 - s))    # a dead sensor; the object wider than the gauge
        past = r["closed"] - r["lowest"] - width / 2 * (1 - s)
        worst = max(worst, g["stall_force_n"] * min(max(past, 0.0), band) / band)
        assert r["final"] == r["max"] and "!GRIP no force reading" in out, (s, r)
    assert ol["large_n"] == pytest.approx(worst, abs=0.02)
    whole = "its whole stall force" if worst >= g["stall_force_n"] - 0.005 else None
    code = open(next(tmp_path.rglob("morph_arm.ino")), encoding="utf-8").read()     # the firmware and the manual say so
    assert f"one at the large end of its size, {spread:g} mm per jaw wider, {whole or f'about {worst:g} N'}, until it opens" in code
    from morph.robot import manual
    assert (f"one at the large end of its size, {spread:.2f} mm per jaw wider, {whole or f'about {worst:g} N'}"
            in manual._grip_sentence(g, "tile"))
    told = [i for i in ep["issues"] if "no torque limit to back up the force sensors" in i]
    assert brief.classify(told) == ({"hobby_gripper"} if told else set())
    if dmg and worst > dmg:          # crushed at the large end with a dead sensor: never without an issue
        assert not ol["covers"] and told, (ol, ep["issues"])
        assert (f"and {'its whole stall force' if worst >= g['stall_force_n'] - 0.005 else f'{worst:g} N per finger'} "
                f"on a tile at the large end of its size, {spread:.2f} mm per jaw wider, above the {dmg:g} N that "
                f"damages it") in told[0], told[0]
    else:
        assert ol["covers"] and not told, ep["issues"]
        w = next(w for w in ep["warnings"] if "stops there and opens (!GRIP)" in w)
        assert (f"a tile at the large end of its size, {spread:.2f} mm per jaw wider, gets "
                + ("its whole stall force" if worst >= g["stall_force_n"] - 0.005 else f"about {worst:g} N per finger")
                + " for that moment") in w, w


@pytest.mark.parametrize("obj", ["raw egg", "soda can"])
def test_hobby_servos_on_the_stock_jaws_say_their_least_close_squeezes_past_what_they_hold(tmp_path, obj):
    """The SO-101's stock jaw on hobby PWM servos: one PCA9685 count is about 0.48 mm at its 62-80 mm lever, so the least
    G can close past the object (two commands) squeezed an egg with 11.04 N, twice the 5.62 N the servo holds
    continuously and above F's own cap, and F couldn't lower it. The plan raised no issue for it, the firmware called
    a can's 11.05 N 'below Morph's 10.82 N target' and the manual told F to lower it. It is now an issue (the kind New
    robot and "make it cheaper" read), and the firmware and manual say the squeeze is two commands, more than the
    target. The jaw is calibrated on a gauge of the object's size against the fixed jaw, as G's squeeze assumes, and
    the one moving jaw takes the object's whole size spread (8 % of its width), not half of it."""
    from morph import electronics
    from morph.robot import manual
    from morph.studio import brief

    d = template("so101", source="so101").with_changes({**{k: v for k, v in object_changes(obj).items()
                                                           if k.startswith("object.")}, "arm.actuator_family": "hobby_pwm"})
    exe, ep = _compile_firmware(tmp_path, d)
    g, ol = ep["grip"], ep["grip"]["open_loop"]
    assert g["jaw"] == "revolute"
    assert ol["spread_mm"] == pytest.approx(electronics.OBJECT_SIZE_SPREAD * d.object.diameter_mm, abs=1e-3)
    assert g["squeeze_n"] > ol["rated_n"] and g["squeeze_n"] > g["limit_n"] and g["squeeze_n"] > g["target_n"]
    told = [i for i in ep["issues"] if "the least it can close past" in i]
    assert len(told) == 1 and brief.classify(told) == {"hobby_gripper"}, ep["issues"]
    assert (f"squeezes about {g['squeeze_n']:g} N per finger, above the {ol['rated_n']:g} N it holds continuously (its "
            f"rated torque)") in told[0], told[0]
    code = open(next(tmp_path.rglob("morph_arm.ino")), encoding="utf-8").read()
    line = next(ln for ln in code.splitlines() if "float GRIP_TARGET_N" in ln)
    assert "two position commands, the least G can close past" in line and "more than Morph's" in line, line
    assert "below Morph's" not in line
    assert "the calibrated zero (jaw on the gauge)" in code
    sent = manual._grip_sentence(g, d.object.name)
    assert (f"(more than Morph's {g['target_n']:g} N target: two position commands are the least it can close past it)"
            in sent and "F can't lower it" in sent and "lower it if the servo hums" not in sent), sent
    r, out = _run_exe(exe, 0.0, 0.0)                         # G alone: two commands past the gauge contact
    assert (r["closed"] - r["lowest"]) * g["lever_mm"] == pytest.approx(ol["squeeze_mm"], abs=0.005), r   # (4-place rad)


# ------------------------------------------------------------------ the grip holds what the robot does with it

@pytest.mark.parametrize("family, slip", [("", 4.74), ("dynamixel", 4.83)])
def test_the_so101_demo_keeps_its_egg_through_the_shake_test_with_a_thin_margin(family, slip):
    """The SO-101 demo (docs/DEMO_SCRIPT.md steps 11-13) holds its egg through the shake test with little to spare:
    4.74 mm of slip, 4.83 mm on Dynamixel servos (step 12), against the 5 mm the test allows. A firmer grip doesn't buy
    margin: the egg creeps along the stock jaw as it seats, 4.53 mm at 11 N and 4.47 mm at 14 N, near the top of its
    1.26-16.67 N window, so the open-loop grip stays at the window's middle (4.58 N), as far from crushing as from
    slipping. If this fails the demo robot gains a shake issue: step 12 shows 7 issues, not 6, and the stock SO-101 no
    longer passes its software checks."""
    d = template("so101", "so101")
    if family:
        d = d.with_changes({"arm.actuator_family": family})
    sz = imported.size(d)
    force = imported.gripper_summary(d, sz)["force"]
    assert force["target_force_n"] == 4.58 and force["window_n"] == [1.26, 16.67]
    asm = imported.build(d, sz, electronics2.plan(d, sz, grip=force))
    m = simulate.run(asm, d, "shake")["metrics"]
    assert m["success"] and m["slip_mm"] == pytest.approx(slip, abs=0.05), m
    assert 5.0 - m["slip_mm"] > 0.1, f"the shake margin is down to {5.0 - m['slip_mm']:.2f} mm"


def test_the_grip_limited_payload_is_what_the_firmwares_hard_stop_holds():
    """The payload sweep's grip_limited_payload_g used the jaws' stall force: 5,913 g for the egg-picker at 82.9 N per
    finger, a squeeze its firmware never makes (GRIP_LIMIT_N is 19 N: the force sensor's range). It is now what
    friction holds at the firmware's hard stop, 1,356 g, and the sweep says which squeeze it counted."""
    d = template("egg-picker")
    sz = arm.size(d)
    ep = electronics2.plan(d, {**sz, "grip_target_n": 2.34})
    asm = arm.build(d, sz, ep)
    assert asm.meta["grip_limit_n"] == ep["grip"]["limit_n"] == 19.0 < asm.joint("gripper").effort
    m = simulate.run(asm, d, "payload_sweep", fast=True)["metrics"]
    assert m["grip_limit_n"] == 19.0
    assert m["grip_limited_payload_g"] == round(2 * d.object.friction * 19.0 / 9.81 * 1000) == 1356



def test_the_grip_holds_a_heavy_robust_object_through_the_robots_own_shake_test(tmp_path):
    """A 370 g can (no damage force): the gripper stage's grasp simulation held it at 2.34 N per finger, half the static
    friction hold m g / (2 mu gain) = 4.65 N (its contour pads cradle the can; the robot's fingers don't), and the plan
    shipped 1.3 x 1.5 x that: 4.56 N. The robot's own shake test then dropped the can (213 mm) while the build said
    every check passed. The hold is now never below the static hold, and an object without a damage force is gripped
    at twice its lower bound (3x the static hold: the shake test loads the grip with about 1.9 g)."""
    from morph import electronics

    d = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none"})
    sz = arm.size(d)
    force = pipeline2.gripper_stage(d, sz, n_trials=1)["force"]       # with its grasp simulation
    static = force["static_hold_n"]
    gain = pipeline2.gripper_stage(d, sz, simulate=False)["analysis"]["wedge_gain"]
    assert static == pytest.approx(electronics.static_hold(d.object, gain), abs=1e-3)
    assert force["sim_hold_n"] < static <= force["hold_force_n"]       # the grasp simulation only ever raises it
    lo = force["window_n"][0]
    assert lo == pytest.approx(1.5 * force["hold_force_n"], abs=0.01)
    assert force["target_force_n"] == pytest.approx(2 * lo, abs=0.01) and force["window_n"][1] is None
    ep = electronics2.plan(d, sz, grip=force)
    asm = arm.build(d, sz, ep)
    doc = simulate.run(asm, d, "shake", fast=True)
    m = doc["metrics"]
    assert m["grip_target_n"] == pytest.approx(force["target_force_n"], abs=0.01)
    assert m["success"] and not m["dropped"] and m["slip_mm"] < 5.0, m


@pytest.mark.parametrize("family, told", [("smart_bus", False), ("dynamixel", False), ("hobby_pwm", True)])
def test_only_a_gripper_with_no_torque_limit_is_told_it_needs_force_sensors(family, told):
    # an egg-picker set to Force sensor: none was "not feasible: sts3215 can reach 82.9 N > object limit 25 N and there is
    # no force sensor: add finger.sensor=fsr402", while its firmware sets the servo's torque limit for the grip (5 % of
    # its stall torque for 4.3 N) and the simulation squeezes with that. A hobby PWM servo has no torque limit: still told
    from morph.studio import brief

    d = template("egg-picker").with_changes({"finger.sensor": "none", "arm.actuator_family": family})
    sz = arm.size(d)
    force = pipeline2.gripper_stage(d, sz, simulate=False)["force"]
    said = any("add finger.sensor=fsr402" in i for i in force["issues"])
    assert said is told, force["issues"]
    ep = electronics2.plan(d, sz, grip=force)
    assert (ep["grip"]["torque_limit"] is None) is told
    pre = brief.preflight(d.flat())["issues"]
    assert any("add finger.sensor=fsr402" in i for i in pre) is told, pre
