"""World mode: the whole pick-and-place path is checked against the room's boxes every 10 ms (morph/robot/simulate.py),
each move that comes within 10 mm tries its alternatives (the approach from above, the release with the jaws opened
less, the way back to rest straight up first), and a move that still passes into a box fails the simulation naming the
move and the box. Simulations without a world are pinned by tests/test_sim_golden.py. Offline, the egg-picker at 1 ms."""

import math

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, simulate  # noqa: E402
from morph.studio import worlds  # noqa: E402
from morph.studio.projects import template  # noqa: E402

MOVE_ORDER = ["approach", "descent", "grasp", "lift", "carry", "set-down", "release", "rise", "return"]


@pytest.fixture(scope="module")
def egg():
    d = template("egg-picker")
    sz = arm.size(d)
    return d, arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34}))


def bench(*obstacles):
    return {"id": "bench", "sha": "test", "surface": {"label": "Bench top", "center": [100.0, 0.0, -12.5],
                                                     "half": [550.0, 600.0, 12.5], "yaw_deg": 0.0},
            "floor_z": -900.0, "obstacles": list(obstacles), "zones": []}


def box(label, center, half, yaw=0.0):
    return {"id": label.lower().replace(" ", "-"), "label": label, "center": [float(v) for v in center],
            "half": [float(v) for v in half], "yaw_deg": float(yaw)}


def bin_beside_the_arc(d, off=20.0, h=90.0):
    """The research's bench-bin: a 60 x 60 x 90 mm bin `off` mm outside the middle of the carry arc."""
    pick, place = np.array([d.task.pick_x_mm, d.task.pick_y_mm]), np.array([d.task.place_x_mm, d.task.place_y_mm])
    a0, a1 = simulate._arc_angles(np.r_[pick, 0], np.r_[place, 0])
    am, rm = (a0 + a1) / 2, 0.5 * (np.linalg.norm(pick) + np.linalg.norm(place)) + off
    return box("Bin", (rm * math.cos(am), rm * math.sin(am), h / 2), (30, 30, h / 2))


def carton_wall(d, off):
    """The research's carton: a 12 x 80 x 40 mm wall across the line from the base, `off` mm past the place spot."""
    place = np.array([d.task.place_x_mm, d.task.place_y_mm])
    a, r = math.atan2(place[1], place[0]), float(np.linalg.norm(place)) + off
    return box("Carton wall", (r * math.cos(a), r * math.sin(a), 20.0), (6, 40, 20), math.degrees(a))


def run(egg, world, monkeypatch=None, **patch):
    d, asm = egg
    if monkeypatch is not None:
        for k, v in patch.items():
            monkeypatch.setattr(simulate, k, v)
    return simulate.run(asm, d, "pick_place", world=world)


def moves(m):
    return {e["move"]: e for e in m["path"]}


def test_bench_bin_20_finds_a_clear_way_round(egg):
    """Acceptance: the bench-bin at 20 mm, where the arc check once said "clear 13.5 mm" while the jaw hit the bin on
    the way back: every move is now checked, and the way back keeps 10 mm or more."""
    d, _ = egg
    m = run(egg, bench(bin_beside_the_arc(d, 20.0)))["metrics"]
    assert m["success"] and not m["world_contacts"] and m["clearance_ok"] and "blocked" not in m, m.get("failure")
    assert [e["move"] for e in m["path"]] == MOVE_ORDER
    assert all(e["clear"] and e["min_clearance_mm"] >= simulate.CLEAR_MM for e in m["path"]), m["path"]
    assert m["detours"]["retreat"]["clear"] and m["detours"]["retreat"]["via"] != "direct"
    assert moves(m)["return"]["min_clearance_mm"] == m["detours"]["retreat"]["min_clearance_mm"]
    assert m["min_clearance_mm"] >= simulate.CLEAR_MM                  # and the simulated run kept it too


def test_the_check_takes_every_10_ms_sample(egg):
    """_check_path's moves cover the plan sample by sample, and each move's clearance is the smallest over all its 10 ms
    samples (as a plain loop over every sample finds it)."""
    d, asm = egg
    rob = simulate._make_robot(asm, d, False, {"world": bench(bin_beside_the_arc(d, 20.0))})
    plan, at = simulate._plan_pick_place(rob, d, False)
    starts = [i for _, i in plan.moves]
    assert starts[0] == 0 and starts == sorted(starts) and [n for n, _ in plan.moves] == MOVE_ORDER
    dd = rob._mj.MjData(rob.model)
    ends = starts[1:] + [len(plan.t)]
    for (name, i0), i1, e in zip(plan.moves, ends, rob.path):
        ref = math.inf
        for k in range(i0, i1):
            held = plan.ph[k] in simulate.CARRIED
            rob.pose_carried(dd, plan.q[k], simulate._held(rob, plan.g[k]), held)
            ref = min(ref, rob.clearance(dd, with_object=held)[0])
        assert e["move"] == name and e["min_clearance_mm"] == math.floor(min(ref, 500.0) * 10.0 + 1e-6) / 10.0, (name, e, ref)
        assert e["clear"] == (ref >= simulate.CLEAR_MM)
    assert np.allclose(np.diff(plan.t), simulate.PLAN_DT)                # one sample every 10 ms


@pytest.mark.parametrize("raw, kept, ok", [(9.96, 9.9, False), (30.68, 30.6, True), (10.0, 10.0, True), (-0.04, -0.1, False)])
def test_the_simulated_clearance_is_kept_rounded_down(egg, raw, kept, ok):
    """The smallest clearance over the simulated frames is kept to 0.1 mm rounded down, never up: 9.96 mm is 9.9 and
    not clear (before: 10.0, "clear"), 30.68 is 30.6, and 0.04 mm into a box is 0.1 mm into it."""
    d, asm = egg
    rob = simulate._make_robot(asm, d, True, {"world": bench(bin_beside_the_arc(d, 20.0))})
    plan, at = simulate._plan_pick_place(rob, d, True)
    run_, met = simulate._run_pick_place(rob, d, plan, at)
    real = rob.clearance
    rob.clearance = lambda dd, with_object=False: (raw, *real(dd, with_object=with_object)[1:])
    m = simulate._world_metrics(rob, d, run_, plan)
    assert m["min_clearance_mm"] == kept and m["clearance_ok"] is ok, m["min_clearance_mm"]


@pytest.mark.parametrize("off", [36.0])
def test_a_carton_wall_too_close_fails_naming_the_set_down(egg, off):
    d, _ = egg
    m = run(egg, bench(carton_wall(d, off)))["metrics"]
    assert not m["success"] and m["world_contacts"] and m["blocked"]["move"] == "set-down"
    assert m["world_contacts"][0]["move"] == "set-down" and m["world_contacts"][0]["phase"] == "place"
    assert m["failure"].startswith("the jaw ") and "hits Carton wall during the set-down at " in m["failure"]
    assert moves(m)["set-down"]["at"]["obstacle"] == "Carton wall" and not moves(m)["set-down"]["clear"]


def test_the_release_opens_less_beside_a_carton_wall(egg, monkeypatch):
    """With the jaws opened fully the hand comes within 8 mm of the wall (research: carton 62 mm out); opened only 4 mm
    past the egg it keeps 10 mm, and the egg is still let go where it should be."""
    d, _ = egg
    world = bench(carton_wall(d, 62.0))
    doc = run(egg, world)
    m = doc["metrics"]
    rel = m["detours"]["release"]
    assert rel == {"via": "opened less", "min_clearance_mm": rel["min_clearance_mm"], "clear": True, "open_mm": 4.0}
    assert m["success"] and m["clearance_ok"] and m["place_error_mm"] < 1.0, m.get("failure")
    obj_z = [f["obj"][2] for f in doc["frames"] if f["t"] > 6.5]
    assert max(obj_z) - min(obj_z) < 1.0                                    # let go: it doesn't rise with the hand
    g = [f["q"][-1] for f in doc["frames"] if f["phase"] == "release"]
    assert max(g) < 4.5 and doc["frames"][-1]["q"][-1] == pytest.approx(doc["frames"][0]["q"][-1], abs=0.5)
    full = run(egg, world, monkeypatch, OPEN_LESS_MM=1e9)["metrics"]         # no partial opening to fall back on
    assert full["detours"]["release"]["via"] == "full" and full["min_clearance_mm"] < simulate.CLEAR_MM


def test_the_way_back_rises_straight_up_first(egg, monkeypatch):
    """A post between the place spot and the rest pose: every way back the old planner had (straight, hand closed,
    along the carry arc) comes within 10 mm or hits it; rising 80 mm first keeps clear."""
    post = box("Post", (203.6, 83.6, 34.8), (20.7, 20.7, 34.8))
    m = run(egg, bench(post))["metrics"]
    back = m["detours"]["retreat"]
    assert back["via"] == "straight up first" and back["rise_mm"] in simulate.LIFTS_MM and back["clear"]
    assert m["success"] and m["clearance_ok"] and all(e["clear"] for e in m["path"]), m["path"]
    before = run(egg, bench(post), monkeypatch, LIFTS_MM=())["metrics"]   # without the rise: what was left
    assert not before["detours"]["retreat"]["clear"] and before["detours"]["retreat"]["via"] != "straight up first"


def test_the_approach_comes_in_from_above(egg, monkeypatch):
    post = box("Post", (151.2, -35.6, 44.5), (9.3, 9.3, 44.5))
    m = run(egg, bench(post))["metrics"]
    there = m["detours"]["approach"]
    assert there["via"] == "from above" and there["clear"] and there["rise_mm"] in simulate.LIFTS_MM
    assert m["success"] and m["clearance_ok"] and moves(m)["approach"]["clear"], m["path"]
    before = run(egg, bench(post), monkeypatch, LIFTS_MM=())["metrics"]
    assert not before["detours"]["approach"]["clear"]


def test_a_box_in_the_way_down_fails_naming_the_descent(egg):
    post = box("Post", (239.9, -134.3, 24.0), (11.8, 11.8, 24.0))
    m = run(egg, bench(post))["metrics"]
    assert not m["success"] and m["blocked"]["move"] == "descent" and m["blocked"]["at"]["obstacle"] == "Post"
    assert "during the descent to the object" in m["failure"] and "Post" in m["failure"]


def test_a_planned_path_into_a_box_fails_without_a_contact(egg, monkeypatch):
    """A plan that passes into a box fails even when the simulated arm, lagging its set-points, happens to miss it."""
    d, asm = egg
    monkeypatch.setattr(simulate, "_world_hits", lambda rob, dd, t, hits: None)      # the run records no contact
    m = simulate.run(asm, d, "pick_place", world=bench(carton_wall(d, 36.0)))["metrics"]
    assert not m["world_contacts"] and not m["success"] and not m["clearance_ok"]
    assert m["failure"].endswith("and no clear way around it was found") and "into Carton wall during the set-down" in m["failure"]


def test_the_starter_rooms_keep_their_plans(egg):
    """The desk, counter and workbench: every move clear and straight, as before the path check."""
    for name in worlds.STARTERS:
        m = run(egg, worlds.sim_world(worlds.starter(name)))["metrics"]
        assert m["success"] and m["clearance_ok"], (name, m.get("failure"))
        assert {k: v["via"] for k, v in m["detours"].items()} == {"approach": "direct", "release": "full", "retreat": "direct"}
        assert all(e["clear"] for e in m["path"]) and m["arc"]["usual"] is True


def test_the_check_reads_the_move(egg):
    """World mode's placement check names the move of a contact, and fails a plan that passes into a box."""
    w = worlds.validate({"id": "bench", "mount": {"position": [0, 0, 0], "yaw_deg": 0}, "surface": {
        "label": "Bench top", "center": [100.0, 0.0, -12.5], "half": [550.0, 600.0, 12.5]},
        "obstacles": [{"label": "Carton wall", "center": [180, 190, 20], "half": [6, 40, 20]}]})
    base = {"world": "bench", "world_sha": worlds.sha(w), "success": False, "reachable": True, "min_clearance_mm": -0.5,
            "min_clearance_at": {"t": 5.0, "link": "jaw_left", "obstacle": "Carton wall"}}
    hit = {**base, "world_contacts": [{"t": 5.031, "link": "jaw_left", "obstacle": "Carton wall", "depth_mm": 0.8,
                                       "move": "set-down", "phase": "place"}]}
    res = worlds.checks(w, {"sims": {"scenarios": {"pick_place": {"metrics": hit}}}})
    c = next(c for c in res["checks"] if c["id"] == "clearance")
    assert c["status"] == "fail" and c["text"] == "The jaw left hits Carton wall during the set-down at 5.0 s."
    blocked = {**base, "min_clearance_mm": 0.4, "world_contacts": [], "blocked": {
        "move": "set-down", "phase": "place", "min_clearance_mm": -4.0, "clear": False,
        "at": {"t": 5.31, "link": "jaw_left", "obstacle": "Carton wall"}}}
    res = worlds.checks(w, {"sims": {"scenarios": {"pick_place": {"metrics": blocked}}}})
    c = next(c for c in res["checks"] if c["id"] == "clearance")
    assert c["status"] == "fail" and res["verdict"] == "blocked"
    assert c["text"] == ("The planned path takes the jaw left 4.0 mm into Carton wall during the set-down (5.3 s), and no "
                         "clear way around it was found.")
    assert worlds.MOVES == simulate.MOVES
