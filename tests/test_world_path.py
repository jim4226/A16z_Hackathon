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


def fine_min(rob, poses, pose, step):
    """The clearance along a path through `poses` posed every `step` of each move between them (joints, the grip and
    the object linearly, the quaternion normalised): the reference the check must never be above."""
    dd = rob._mj.MjData(rob.model)
    best = (math.inf, None)
    for a, b in zip(poses, poses[1:] + poses[-1:]):
        n = max(1, int(round((b["t"] - a["t"]) / step))) if b is not a else 1
        for k in range(n):
            u = k / n
            p = {"t": a["t"] + (b["t"] - a["t"]) * u, "q": (1 - u) * a["q"] + u * b["q"]}
            if "obj" in a:
                o = (1 - u) * a["obj"] + u * b["obj"]
                p["obj"] = np.r_[o[:3], o[3:] / max(float(np.linalg.norm(o[3:])), 1e-9)]
            pose(dd, p, a["held"])
            c = rob.clearance(dd, with_object=a["held"])[0]
            if c < best[0]:
                best = (c, p["t"])
    return best


def plan_poses(rob, plan, i0, i1):
    return [{"t": float(plan.t[k]), "q": np.r_[plan.q[k], simulate._held(rob, plan.g[k])],
             "held": plan.ph[min(k, i1 - 1)] in simulate.CARRIED} for k in range(i0, min(i1 + 1, len(plan.t)))]


def test_the_check_takes_every_10_ms_sample(egg):
    """_check_path's moves cover the plan sample by sample, and each move's clearance is no more than the smallest over
    all its 10 ms samples (as a plain loop over every sample finds it) nor than the move posed every 0.5 ms between
    them (R6: between two samples a jaw moves up to several millimetres), and within 0.2 mm of the latter."""
    d, asm = egg
    rob = simulate._make_robot(asm, d, False, {"world": bench(bin_beside_the_arc(d, 20.0))})
    plan, at = simulate._plan_pick_place(rob, d, False)
    starts = [i for _, i in plan.moves]
    assert starts[0] == 0 and starts == sorted(starts) and [n for n, _ in plan.moves] == MOVE_ORDER
    dd = rob._mj.MjData(rob.model)
    ends = starts[1:] + [len(plan.t)]
    pose = lambda dd, p, held: rob.pose_carried(dd, p["q"][:-1], p["q"][-1], held)     # noqa: E731
    for (name, i0), i1, e in zip(plan.moves, ends, rob.path):
        ref = math.inf
        for k in range(i0, i1):
            held = plan.ph[k] in simulate.CARRIED
            rob.pose_carried(dd, plan.q[k], simulate._held(rob, plan.g[k]), held)
            ref = min(ref, rob.clearance(dd, with_object=held)[0])
        assert e["move"] == name and e["min_clearance_mm"] <= math.floor(min(ref, 500.0) * 10.0 + 1e-6) / 10.0, (name, e, ref)
        if name in ("carry", "return"):
            fine = fine_min(rob, plan_poses(rob, plan, i0, i1), pose, 0.0005)[0]
            assert fine - 0.2 <= e["min_clearance_mm"] <= fine, (name, e, fine)
        assert e["clear"] == (e["min_clearance_mm"] >= simulate.CLEAR_MM)
    assert np.allclose(np.diff(plan.t), simulate.PLAN_DT)                # one sample every 10 ms


@pytest.mark.parametrize("raw, kept, ok", [(9.96, 9.9, False), (30.68, 30.6, True), (10.0, 10.0, True), (-0.04, -0.1, False)])
def test_the_simulated_clearance_is_kept_rounded_down(egg, raw, kept, ok, monkeypatch):
    """The smallest clearance along the simulated run is kept to 0.1 mm rounded down, never up: 9.96 mm is 9.9 and
    not clear (before: 10.0, "clear"), 30.68 is 30.6, and 0.04 mm into a box is 0.1 mm into it. (The arm held still
    between frames here, so nothing between them comes closer; the plan's moves are checked the same way.)"""
    d, asm = egg
    rob = simulate._make_robot(asm, d, True, {"world": bench(bin_beside_the_arc(d, 20.0))})
    plan, at = simulate._plan_pick_place(rob, d, True)
    run_, met = simulate._run_pick_place(rob, d, plan, at)
    real = rob.clearance
    rob.clearance = lambda dd, with_object=False, distmax=0.5: (raw, *real(dd, with_object=with_object)[1:])
    monkeypatch.setattr(rob.motion, "bound", lambda a, b: np.zeros(len(rob.motion.geoms)))
    simulate._check_path(rob, plan)
    m = simulate._world_metrics(rob, d, run_, plan)
    assert m["min_clearance_mm"] == kept and m["clearance_ok"] is ok, m["min_clearance_mm"]


def run_and_frames(egg, world, monkeypatch):
    """(metrics, robot, run) of a world-mode pick and place."""
    got = {}
    real = simulate._world_metrics

    def spy(rob, design, run_, plan):
        got.update(rob=rob, run=run_)
        return real(rob, design, run_, plan)
    monkeypatch.setattr(simulate, "_world_metrics", spy)
    m = run(egg, world)["metrics"]
    return m, got["rob"], got["run"]


def frame_pose(rob):
    def pose(dd, p, held):
        dd.qpos[:] = 0
        dd.qpos[rob.qadr] = p["q"][:rob.n]
        if rob.g_q:
            rob.set_grip(dd, p["q"][-1])
        dd.qpos[rob.obj_q:rob.obj_q + 3] = np.asarray(p["obj"][:3]) * simulate.MM
        dd.qpos[rob.obj_q + 3:rob.obj_q + 7] = simulate._q_body(p["obj"][3:7])
        rob._mj.mj_kinematics(rob.model, dd)
    return pose


@pytest.mark.parametrize("which", ["bin 18.5", "bracket"])
def test_the_runs_clearance_counts_the_motion_between_its_frames(egg, which, monkeypatch):
    """R6: a jaw point moves up to 19.5 mm between two of the run's frames (30 a second). With the bench bin 18.5 mm
    outside the carry arc the frames said 10.3 mm ("At least 10 mm", clearance_ok) while the run posed every 2 ms comes
    within 9.956 mm, and the plan's own way back was 9.9 mm and not clear; beside a 10 mm bracket the frames said
    12.4 mm, the run 10.916 mm. The kept clearance is now never more than the run posed every 2 ms (and within 0.2 mm
    of it), and clearance_ok is false when a planned move isn't clear."""
    d, _ = egg
    ob = bin_beside_the_arc(d, 18.5) if which == "bin 18.5" else box("Bracket", (260.56, 18.71, 76.18), (5.0, 5.0, 5.0))
    m, rob, run_ = run_and_frames(egg, bench(ob), monkeypatch)
    poses = [{"t": float(f["t"]), "q": np.asarray(f["q"], float), "obj": np.asarray(f["obj"], float),
              "held": f["phase"] in simulate.CARRIED} for f in run_.frames]
    truth, t = fine_min(rob, poses, frame_pose(rob), 0.002)
    assert truth - 0.2 <= m["min_clearance_mm"] <= truth, (m["min_clearance_mm"], truth, t)
    assert m["min_clearance_mm"] <= min(e["min_clearance_mm"] for e in m["path"])
    assert m["clearance_ok"] == (all(e["clear"] for e in m["path"]) and m["min_clearance_mm"] >= simulate.CLEAR_MM)
    if which == "bin 18.5":
        assert not m["clearance_ok"] and m["min_clearance_mm"] < simulate.CLEAR_MM and m["success"]
        w = worlds.validate({"id": "bench", "mount": {"position": [0, 0, 0], "yaw_deg": 0}, "surface": bench()["surface"],
                             "obstacles": [ob]})
        res = worlds.checks(w, {"sims": {"scenarios": {"pick_place": {"metrics": {**m, "world": "bench",
                                                                                   "world_sha": worlds.sha(w)}}}}})
        c = next(c for c in res["checks"] if c["id"] == "clearance")
        assert c["status"] == "warn" and c["text"].startswith("Only 9.") and "At least" not in c["text"], c


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


def test_the_workspace_measures_the_arm_itself_not_its_tool_point(egg):
    """R6: the overhead and people checks used the tool point's highest and furthest of 4000 random poses (567.1 and
    568.0 mm on the egg-picker) while its fingers reach about 20 mm further. The workspace scenario now keeps the arm's
    own top and furthest point (arm_top_mm, arm_reach_mm): at least what any of 3000 random poses, the jaws at either
    end of their range, puts any vertex of the robot's collision shapes at, and not much more."""
    d, asm = egg
    m = simulate.run(asm, d, "workspace")["metrics"]
    rob = simulate._make_robot(asm, d, False, {})
    mm, dd = rob.model, rob._mj.MjData(rob.model)
    V, G = [], []
    for g in rob.robot_geoms:
        if mm.geom_type[g] == rob._mj.mjtGeom.mjGEOM_MESH:
            a, n = mm.mesh_vertadr[mm.geom_dataid[g]], mm.mesh_vertnum[mm.geom_dataid[g]]
            V.append(np.asarray(mm.mesh_vert[a:a + n], float))
            G.append(np.full(n, g))
    V, G = np.vstack(V), np.concatenate(G)
    jaw = int(np.nonzero(mm.jnt_qposadr == rob.g_q[0])[0][0])
    links = [mm.body(n).id for n in rob.asm.ordered_links() if mm.body(n).id > 0]
    rng = np.random.default_rng(11)
    top = far = 0.0
    for q in rng.uniform(rob.lo, rob.hi, size=(3000, rob.n)):
        for g in mm.jnt_range[jaw] / rob.g_scale:
            dd.qpos[:] = 0
            dd.qpos[rob.qadr] = q
            rob.set_grip(dd, g)
            rob._mj.mj_kinematics(mm, dd)
            if dd.xpos[links, 2].min() < -1e-3 or dd.site_xpos[rob.tcp_site][2] < 0.0:
                continue
            P = (np.einsum("nij,nj->ni", dd.geom_xmat[G].reshape(-1, 3, 3), V) + dd.geom_xpos[G]) / simulate.MM
            top, far = max(top, float(P[:, 2].max())), max(far, float(np.linalg.norm(P, axis=1).max()))
    assert top <= m["arm_top_mm"] <= top + 5.0 and far <= m["arm_reach_mm"] <= far + 5.0, (m, top, far)
    assert m["arm_top_mm"] > m["max_height_mm"] + 10.0 and m["arm_reach_mm"] > m["reach_3d_mm"] + 10.0, m
