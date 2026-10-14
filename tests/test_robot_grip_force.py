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
    assert force["window_n"] == [0.25, 2.0] and force["target_force_n"] == 0.5 and force["limit_n"] == 2.5
    assert _firmware_grip(code)[0] == 0.5


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
