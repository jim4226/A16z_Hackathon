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
    assert [w for w in pwm[2]["warnings"] if "G closes the jaws past the object with all its torque" in w]
    assert any("no torque limit" in i for i in imported.gripper_summary(pwm[0], pwm[1])["force"]["issues"])


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


def test_a_sensorless_hobby_gripper_is_simulated_squeezing_with_all_its_torque():
    """A hobby PWM servo has no torque limit: without force sensors its firmware closes past the object with all its
    torque, so the simulation squeezes with the servo's stall force per finger, not the target it can't regulate."""
    d = Design().with_changes({**EGG_ROBOT, **object_changes("soda can"), "finger.sensor": "none",
                               "arm.actuator_family": "hobby_pwm"})
    sz = arm.size(d)
    ep = electronics2.plan(d, sz, grip=pipeline2.gripper_stage(d, sz, simulate=False)["force"])
    g = ep["grip"]
    assert g["torque_limit"] is None and g["stall_force_n"] > g["target_n"]
    asm = arm.build(d, sz, ep)
    assert asm.meta["grip_squeeze_n"] == g["stall_force_n"]
    info, _ = simulate._describe(asm, d, simulate._opts(None))
    assert info.grip_force_n == pytest.approx(min(g["stall_force_n"], asm.joint("gripper").effort))
    assert [w for w in ep["warnings"] if f"about {g['stall_force_n']:g} N per finger" in w]


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


def _run_exe(exe, newtons_per_unit, offset=0.0):
    """Run the compiled sketch's grip loop: a force sensor reading `newtons_per_unit` per unit closed past the object,
    which meets the jaws `offset` units past the calibrated zero (an object narrower than the gauge)."""
    import subprocess

    out = subprocess.run([str(exe), str(newtons_per_unit), str(offset)], capture_output=True, text=True, timeout=120).stdout
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


# ------------------------------------------------------------------ the grip holds what the robot does with it

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
