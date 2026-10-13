"""World mode in the whole-robot simulation (morph/robot/simulate.py): room boxes, clearance-aware carry arc,
contacts and clearance metrics; without a world nothing changes. Offline, about 30 s."""

import json

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.cli import EGG_ROBOT  # noqa: E402
from morph.robot import arm, simulate  # noqa: E402
from morph.spec import Design  # noqa: E402
from morph.studio import worlds  # noqa: E402


@pytest.fixture(scope="module")
def egg():
    d = Design().with_changes(EGG_ROBOT)
    return d, arm.build(d, arm.size(d))


def _table_world(obstacles):
    """The plain 1.1 x 1.2 m table as a world surface, plus obstacle boxes (robot base frame, mm)."""
    return {"id": "proto", "sha": "test", "surface": {"label": "Table", "center": [100.0, 0.0, -12.0],
                                                      "half": [550.0, 600.0, 12.0], "yaw_deg": 0.0},
            "floor_z": -750.0, "obstacles": obstacles, "zones": []}


def _bin_on_the_arc(design, h=90.0):
    """The research prototype: a 60 x 60 x 90 mm box just outside the middle of the egg-picker's carry arc."""
    pick = np.array([design.task.pick_x_mm, design.task.pick_y_mm])
    place = np.array([design.task.place_x_mm, design.task.place_y_mm])
    a0, a1 = simulate._arc_angles(np.r_[pick, 0], np.r_[place, 0])
    am = (a0 + a1) / 2
    rm = 0.5 * (np.linalg.norm(pick) + np.linalg.norm(place)) + 20
    return {"id": "bin", "label": "Bin", "center": [rm * np.cos(am), rm * np.sin(am), h / 2], "half": [30.0, 30.0, h / 2],
            "yaw_deg": 0.0}


def test_no_world_is_unchanged(egg):
    d, asm = egg
    xml = simulate.build_mjcf(asm, d)
    assert xml == simulate.build_mjcf(asm, d, {"world": None})
    assert "world_" not in xml and "sim_floor" not in xml and "table_world" not in xml
    assert 'name="sim_table" type="box" pos="0.1 0 -0.012" size="0.55 0.6 0.012"' in xml
    a = simulate.run(asm, d, "pick_place", fast=True)
    b = simulate.run(asm, d, "pick_place", fast=True, world=None)
    for doc in (a, b):
        doc["info"].pop("wall_s")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert "world" not in a and not {"world_contacts", "min_clearance_mm", "clearance_ok"} & set(a["metrics"])


def test_world_boxes_touch_the_robot_and_object_only(egg):
    d, asm = egg
    w = _table_world([_bin_on_the_arc(d)])
    m = mujoco.MjModel.from_xml_string(simulate.build_mjcf(asm, d, {"world": w}))
    g = m.geom("world_0")
    assert simulate.B_WORLD == 64 and int(m.geom_contype[g.id]) == 64
    aff = int(m.geom_conaffinity[g.id])
    assert aff == simulate.B_OBJ | simulate.B_ARM | simulate.B_JAW | simulate.B_PROX | simulate.B_NOOBJ
    table = m.geom("sim_table")
    assert not (int(m.geom_contype[table.id]) & aff) and not (64 & int(m.geom_conaffinity[table.id]))
    assert np.allclose(m.geom_size[table.id], [0.55, 0.6, 0.012])            # the mount surface sets the table
    assert np.isclose(m.geom_pos[m.geom("sim_floor").id][2], -0.75)
    obj = m.geom("sim_object")
    assert int(m.geom_contype[obj.id]) & aff                                  # the object can hit a box


def test_prototype_box_on_the_carry_arc(egg, monkeypatch):
    """The current plan (the default arc) fails on a 9 cm box; the clearance-aware arc places within ~1 mm with
    10 mm or more to spare (research: 0.44 mm, 12.4 mm)."""
    d, asm = egg
    w = _table_world([_bin_on_the_arc(d)])
    aware = simulate._clear_arc
    monkeypatch.setattr(simulate, "_clear_arc", simulate._default_arc)      # the plan the plain table gets
    old = simulate.run(asm, d, "pick_place", fast=True, world=w)["metrics"]
    assert not old["success"] and old["world_contacts"] and old["place_error_mm"] > 50
    assert "hits Bin" in old["failure"] and not old["clearance_ok"] and old["min_clearance_mm"] < 0
    monkeypatch.setattr(simulate, "_clear_arc", aware)
    doc = simulate.run(asm, d, "pick_place", fast=True, world=w)
    m = doc["metrics"]
    assert m["success"] and not m["world_contacts"] and m["clearance_ok"]
    assert m["place_error_mm"] <= 1.5
    assert m["min_clearance_mm"] >= 10.0
    assert m["arc"]["clear"] and m["arc"]["min_clearance_mm"] >= 10.0
    assert m["arc"]["usual"] is False                 # not the plain table's arc: the page says it was changed for the room
    assert m["world"] == "proto" and m["min_clearance_at"]["obstacle"] == "Bin"
    assert doc["world"]["obstacles"][0]["label"] == "Bin"


@pytest.mark.parametrize("h", [60.0, 90.0])
def test_full_simulation_goes_back_to_rest_around_the_box(egg, h):
    """The Studio runs full simulations, which end with the move back to the rest pose: straight there it swept through
    the box the carry arc had just avoided (a contact at 6.7 s failed the whole run). It now goes back along the carry
    arc with the hand closed to the object's width, then to rest, keeping 10 mm."""
    d, asm = egg
    w = _table_world([_bin_on_the_arc(d, h)])
    doc = simulate.run(asm, d, "pick_place", fast=False, world=w)
    m = doc["metrics"]
    assert m["success"] and not m["world_contacts"] and m["clearance_ok"], (m.get("failure"), m.get("detours"))
    assert m["min_clearance_mm"] >= simulate.CLEAR_MM
    back = m["detours"]["retreat"]
    assert back["via"] == "carry path" and back["clear"] and back["min_clearance_mm"] >= simulate.CLEAR_MM
    assert m["detours"]["approach"]["via"] == "direct" and m["detours"]["approach"]["clear"]
    f = doc["frames"]
    assert f[-1]["phase"] == "retreat" and np.allclose(f[-1]["q"][:5], f[0]["q"][:5], atol=0.05)   # back at rest
    assert f[-1]["q"][-1] == pytest.approx(f[0]["q"][-1], abs=0.5)                                    # hand open again
    peak = lambda ph: max(max(abs(x) for x in fr["tau"][:5]) for fr in f if fr["phase"] == ph)    # noqa: E731
    assert peak("retreat") <= 1.15 * peak("move")        # back along the arc at the speed it was carried over, no faster
    assert m["cycle_time_s"] == simulate.run(asm, d, "pick_place", fast=False, world=_table_world([]))["metrics"]["cycle_time_s"]


def test_a_box_where_the_arm_rests_is_named_as_such(egg):
    d, asm = egg
    m = simulate.run(asm, d, "pick_place", fast=False, world=_table_world([_bin_on_the_arc(d, 200.0)]))["metrics"]
    assert not m["success"] and m["world_contacts"][0]["t"] < 0.05
    assert m["failure"].endswith("touches Bin in the arm's starting pose")


def test_world_scenarios(egg, tmp_path):
    d, asm = egg
    w = _table_world([_bin_on_the_arc(d)])
    res = simulate.run_all(asm, d, str(tmp_path), fast=True, world=w)
    sc = res["scenarios"]
    assert all(s["metrics"]["success"] for s in sc.values()), {k: s["metrics"].get("error") for k, s in sc.items()}
    assert sc["payload_sweep"]["metrics"]["world_ignored"]                    # a capacity test ignores the room
    assert 0.5 < sc["workspace"]["metrics"]["usable_workspace_fraction"] <= 1.0
    with open(tmp_path / "sim_workspace.json", encoding="utf-8") as fh:
        ws = json.load(fh)
    assert ws["world"]["id"] == "proto" and ws["points"]
    with open(tmp_path / "sim_shake.json", encoding="utf-8") as fh:
        assert json.load(fh)["metrics"]["world_contacts"] == []


def test_workspace_drops_poses_inside_a_box(egg):
    d, asm = egg
    wall = {"id": "wall", "label": "Wall", "center": [150.0, 0.0, 300.0], "half": [10.0, 600.0, 300.0], "yaw_deg": 0.0}
    m = simulate.run(asm, d, "workspace", fast=True, world=_table_world([wall]))["metrics"]
    assert m["usable_workspace_fraction"] < 0.8


def test_contact_fails_pick_place(egg):
    d, asm = egg
    block = {"id": "crate", "label": "Crate", "center": [d.task.place_x_mm, d.task.place_y_mm, 60.0],
             "half": [40.0, 40.0, 60.0], "yaw_deg": 0.0}
    m = simulate.run(asm, d, "pick_place", fast=True, world=_table_world([block]))["metrics"]
    assert not m["success"] and m["world_contacts"]
    assert m["world_contacts"][0]["obstacle"] == "Crate" and "Crate" in m["failure"]


def test_world_without_boxes_still_says_where_it_ran(egg):
    d, asm = egg
    m = simulate.run(asm, d, "pick_place", fast=True, world=_table_world([]))["metrics"]
    assert m["success"] and m["world"] == "proto" and m["world_sha"] == "test" and m["world_contacts"] == []
    assert "min_clearance_mm" not in m                                       # nothing to measure against


def test_old_mujoco_says_what_world_mode_needs(egg, tmp_path, monkeypatch):
    d, asm = egg
    monkeypatch.delattr(mujoco, "mj_geomDistance")                           # as before MuJoCo 3.1.6
    with pytest.raises(RuntimeError, match="MuJoCo 3.1.6"):
        simulate.run(asm, d, "pick_place", fast=True, world=_table_world([_bin_on_the_arc(d)]))
    res = simulate.run_all(asm, d, str(tmp_path), scenarios=["pick_place"], fast=True, world=_table_world([_bin_on_the_arc(d)]))
    assert "MuJoCo 3.1.6" in res["scenarios"]["pick_place"]["metrics"]["error"]    # the Studio shows why


def test_egg_picker_succeeds_on_the_desk(egg):
    d, asm = egg
    w = worlds.sim_world(worlds.starter("desk"))
    doc = simulate.run(asm, d, "pick_place", fast=True, world=w)
    m = doc["metrics"]
    assert m["success"], m.get("failure")
    assert m["clearance_ok"] and m["min_clearance_mm"] >= 10.0 and not m["world_contacts"]
    assert m["arc"]["clear"] and m["arc"]["usual"] is True   # the desk keeps the plain table's carry arc (nothing to say)
    assert abs(doc["targets"]["place"][2] - d.object.diameter_mm / 2) < 1e-6      # resting on the desk top (z = 0)
    assert abs(doc["frames"][-1]["obj"][2] - d.object.diameter_mm / 2) < 3.0
