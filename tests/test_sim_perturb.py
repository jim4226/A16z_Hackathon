"""The robustness hook in the whole-robot simulation (simulate.py perturb=): absent keys are nominal, the nominal model
comes back bit for bit, every change reaches the model, the plan stays nominal, and the egg turned alone reproduces
the tech lead's independent yaw sweep (the product's own _run_one with only the egg's start yaw changed). Without
perturb nothing changes at all: tests/test_sim_golden.py. MORPH_SLOW=1 adds the whole 61-angle sweep."""

import json
import math
import os
import pickle

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, simulate  # noqa: E402
from morph.studio.projects import template  # noqa: E402

SLOW = os.environ.get("MORPH_SLOW") == "1"
# The tech lead's sweep (research, egg-picker r009 = this design and model): place error (mm) and outcome by yaw
# offset, at 2 ms and at 1 ms steps, with the full plan.
SWEEP_2MS = {0.0: (True, 0.1), 4.0: (False, 177.83), 12.0: (True, 0.1)}
SWEEP_1MS_FAILS = [-7.5, -6.5, -5.0, -4.5, -4.0, -3.5, -3.0, -2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.5, 7.0, 7.5]


@pytest.fixture(scope="module")
def egg():
    d = template("egg-picker")
    sz = arm.size(d)
    return d, arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34}))


@pytest.fixture(scope="module")
def fast_robot(egg):
    d, asm = egg
    return simulate._make_robot(asm, d, True, {})


def _plain(doc):
    doc = json.loads(json.dumps(doc))
    doc["info"].pop("wall_s", None)
    return doc


def test_identity_perturbation_is_the_nominal_run(egg):
    d, asm = egg
    a = simulate.run(asm, d, "pick_place", fast=True)
    same = {"off_across_mm": 0.0, "off_along_mm": 0.0, "yaw_deg": 0.0, "size_scale": 1.0, "mass_g": d.object.mass_g,
            "friction_scale": 1.0, "grip_scale": 1.0, "torque_scale": 1.0}
    b = simulate.run(asm, d, "pick_place", fast=True, perturb=same)
    assert _plain(a) == _plain(b)
    with pytest.raises(ValueError):
        simulate.run(asm, d, "shake", fast=True, perturb={"yaw_deg": 1.0})


def test_every_change_reaches_the_model_and_the_nominal_model_comes_back(fast_robot):
    rob = fast_robot
    m = rob.model
    before = pickle.dumps(m)
    g = rob.obj_geom
    jaws = np.nonzero(m.geom_contype == simulate.B_JAW)[0]
    size0, mass0, fr0 = m.geom_size[g].copy(), float(m.body_mass[rob.obj]), m.geom_friction[g, 0]
    grip0, arm0 = m.actuator_forcerange[rob.g_act].copy(), m.actuator_forcerange[rob.aid].copy()
    gain0, bias0 = m.actuator_gainprm[rob.aid, 0].copy(), m.actuator_biasprm[rob.aid, 1].copy()
    p = {"size_scale": 1.08, "mass_g": 72.0, "friction_scale": 0.7, "grip_scale": 1.25, "torque_scale": 0.7,
         "timestep": 0.0005, "impratio": 1.0}
    with simulate._perturbed(rob, p):
        assert np.allclose(m.geom_size[g, :2], [size0[0] * 1.08, (57 * 1.08 / 2 - 22 * 1.08) * 1e-3])
        assert m.body_mass[rob.obj] == pytest.approx(0.072) and mass0 == pytest.approx(0.06)
        assert m.geom_friction[g, 0] == pytest.approx(fr0 * 0.7) and np.allclose(m.geom_friction[jaws, 0], fr0 * 0.7)
        assert np.allclose(m.actuator_forcerange[rob.g_act], grip0 * 1.25)
        assert np.allclose(m.actuator_forcerange[rob.aid], arm0 * 0.7)
        assert np.allclose(m.actuator_gainprm[rob.aid, 0], gain0 * 0.7)
        assert np.allclose(m.actuator_biasprm[rob.aid, 1], bias0 * 0.7)
        assert m.opt.timestep == 0.0005 and m.opt.impratio == 1.0
    assert pickle.dumps(m) == before
    with pytest.raises(RuntimeError), simulate._perturbed(rob, p):
        raise RuntimeError("a run that fails still puts the model back")
    assert pickle.dumps(m) == before


def test_the_room_moves_against_the_mount_error_and_each_box_by_its_own(egg):
    d, asm = egg
    world = {"id": "two-boxes", "sha": "t", "surface": {"label": "Table", "center": [100.0, 0.0, -12.0],
                                                         "half": [550.0, 600.0, 12.0], "yaw_deg": 0.0},
             "floor_z": -750.0, "zones": [],
             "obstacles": [{"id": "a", "label": "A", "center": [300.0, 0.0, 40.0], "half": [20.0, 20.0, 40.0], "yaw_deg": 0},
                           {"id": "b", "label": "B", "center": [0.0, 300.0, 40.0], "half": [20.0, 20.0, 40.0], "yaw_deg": 0}]}
    rob = simulate._make_robot(asm, d, True, {"world": world})
    m = rob.model
    before = pickle.dumps(m)
    a, b = rob.world_geoms
    p = {"mount_dx_mm": 10.0, "mount_dy_mm": -4.0, "mount_yaw_deg": 90.0, "box_offsets_mm": [[0.0, 0.0], [3.0, 4.0]]}
    with simulate._perturbed(rob, p):
        # a robot mounted 10 mm along x and turned 90 degrees sees the room turned back and shifted the other way
        assert np.allclose(m.geom_pos[a] * 1e3, [0.0 - 10.0, -300.0 + 4.0, 40.0], atol=1e-9)
        assert np.allclose(m.geom_pos[b] * 1e3, [300.0 - 10.0 + 3.0, 0.0 + 4.0 + 4.0, 40.0], atol=1e-9)
        assert abs(abs(m.geom_quat[a][0]) - math.cos(math.pi / 4)) < 1e-12
    assert pickle.dumps(m) == before


def test_a_start_off_its_mark_is_measured_from_the_object_axis(egg, fast_robot):
    d, rob = egg[0], fast_robot
    x0, y0 = d.task.pick_x_mm, d.task.pick_y_mm
    yaw = math.atan2(y0, x0)
    x, y, yw, z = simulate._perturb_start(rob, {"off_across_mm": 5.0, "off_along_mm": -2.0, "yaw_deg": 10.0,
                                                "size_scale": 0.9}, x0, y0, yaw)
    ax = np.array([-math.sin(yaw), math.cos(yaw)])          # the egg's long axis lies tangential to the base
    across = np.array([-ax[1], ax[0]])
    assert np.allclose([x - x0, y - y0], 5.0 * across - 2.0 * ax)
    assert yw == pytest.approx(yaw + math.radians(10.0)) and z == pytest.approx(22.0 * 0.9 + 0.2)
    assert simulate._perturb_start(rob, {}, x0, y0, yaw) == (x0, y0, yaw, rob.R + 0.2)


def test_the_egg_turned_alone_reproduces_the_independent_sweep(egg):
    """The research case: the nominal plan jams at release when the egg starts turned 4 degrees (the egg is carried
    off), at 0 and 12 degrees it places. Same numbers as the tech lead's sweep, to the hundredth of a millimetre."""
    d, asm = egg
    rob = simulate._make_robot(asm, d, False, {"timestep": 0.002})
    plan, at = simulate._plan_pick_place(rob, d, False)
    for yaw, (ok, err) in SWEEP_2MS.items():
        pl = simulate._Plan.__new__(simulate._Plan)
        vars(pl).update(vars(plan))
        pl.markers = [dict(mk) for mk in plan.markers]
        with simulate._perturbed(rob, {"yaw_deg": yaw}):
            _, metrics = simulate._run_pick_place(rob, d, pl, at, {"yaw_deg": yaw})
        assert (metrics["success"], metrics["place_error_mm"]) == (ok, err), yaw
    assert plan.markers == simulate._plan_pick_place(rob, d, False)[0].markers       # runs leave the plan alone
    doc = simulate.run(asm, d, "pick_place", timestep=0.002, perturb={"yaw_deg": 4.0})
    assert not doc["metrics"]["success"] and doc["metrics"]["place_error_mm"] == 177.83


def test_the_servo_model_at_nominal_settings_still_places(egg):
    """Encoder quantization (4096 counts), set-points every step, no noise and no deadband: the nominal grasp still
    places within a millimetre of where the plain loop places it."""
    d, asm = egg
    a = simulate.run(asm, d, "pick_place", fast=True)["metrics"]
    b = simulate.run(asm, d, "pick_place", fast=True, perturb={"encoder_cpr": 4096, "rate_hz": 500.0,
                                                               "encoder_noise": 0.0, "deadband_deg": 0.0})["metrics"]
    assert a["success"] and b["success"] and abs(a["place_error_mm"] - b["place_error_mm"]) < 1.0


@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: the whole 61-angle sweep at 1 ms (about a minute)")
def test_the_whole_sweep_at_1ms_and_with_softer_friction(egg):
    d, asm = egg
    rob = simulate._make_robot(asm, d, False, {})
    plan, at = simulate._plan_pick_place(rob, d, False)
    fails, soft = [], []
    for yaw in [round(-15 + 0.5 * i, 1) for i in range(61)]:
        for extra, out in (({}, fails), ({"impratio": 1.0}, soft)):
            p = {"yaw_deg": yaw, **extra}
            pl = simulate._Plan.__new__(simulate._Plan)
            vars(pl).update(vars(plan))
            pl.markers = [dict(mk) for mk in plan.markers]
            with simulate._perturbed(rob, p):
                _, metrics = simulate._run_pick_place(rob, d, pl, at, p)
            if not metrics["success"]:
                out.append(yaw)
    assert fails == SWEEP_1MS_FAILS and soft == []
