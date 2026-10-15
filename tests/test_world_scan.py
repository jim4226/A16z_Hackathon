"""The robot's swept path against an uploaded room scan (worlds.scan_clearance), the inspect check that only warns, and
"Add box here" (H3). The scan near the arm is covered by its own surface points in a k-d tree and refined with exact
point-triangle distances: on scans made of the very boxes a world holds, the distances agree with MuJoCo's
mj_geomDistance pose by pose. Hostile scans (too many triangles, gigantic or degenerate ones, NaN and infinite corners,
faces naming missing corners, nothing near) answer with a reason or a number, never a crash. Offline, fast simulations;
MORPH_SLOW=1 adds the generated room of a million triangles (under 20 s)."""

import json
import math
import os
import sys
import time

import numpy as np
import pytest

pytest.importorskip("mujoco")
trimesh = pytest.importorskip("trimesh")

from morph.robot import arm, electronics2, simulate as S  # noqa: E402
from morph.studio import worlds  # noqa: E402
from morph.studio.projects import template  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_world_checks import ROBOT, design, results  # noqa: E402
from test_world_path import bin_beside_the_arc, box, carton_wall  # noqa: E402

SLOW = os.environ.get("MORPH_SLOW") == "1"
SURFACE = {"label": "Bench top", "center": [100.0, 0.0, -12.5], "half": [550.0, 600.0, 12.5], "yaw_deg": 0.0}
ROOM = {"mount": {"position": [0.0, 0.0, 0.0], "yaw_deg": 0.0}, "surface": SURFACE, "units": "m", "up": "y", "scale": 1.0}
SLAB = {"center": [100.0, 0.0, -12.5], "half": [550.0, 600.0, 12.5], "yaw_deg": 0.0}          # the scanned bench top
POST = {"id": "post", "label": "Post", "center": [231.4, 95.0, 34.8], "half": [20.7, 20.7, 34.8], "yaw_deg": 0.0}
EGG = template("egg-picker")


def box_mesh(b):
    m = trimesh.creation.box(extents=2 * np.asarray(b["half"], float))
    a = math.radians(b.get("yaw_deg", 0.0))
    T = np.eye(4)
    T[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    T[:3, 3] = b["center"]
    m.apply_transform(T)
    return m


def glb(path, V, F):
    """A scan as a phone exports it: one .glb, y up, metres (V in the world frame: mm, z up)."""
    V = np.asarray(V, float) / 1000.0
    m = trimesh.Trimesh(np.c_[V[:, 0], V[:, 2], -V[:, 1]], np.asarray(F), process=False)
    with open(path, "wb") as fh:
        fh.write(m.export(file_type="glb"))
    return str(path)


def boxes_glb(path, boxes):
    m = trimesh.util.concatenate([box_mesh(b) for b in boxes])
    return glb(path, m.vertices, m.faces)


def measure(*a, **k):
    """scan_clearance with time to spare: these tests are about the answer (the 20 s budget is the acceptance test's)."""
    k.setdefault("time_limit_s", 300.0)
    return worlds.scan_clearance(*a, **k)


_SIMS = {}


def simulated(obstacles=(), fast=True):
    """(robot, sim_pick_place document, metrics): the egg-picker on the bench with these grey boxes."""
    key = (json.dumps(obstacles, sort_keys=True), fast)
    if key not in _SIMS:
        sz = arm.size(EGG)
        asm = arm.build(EGG, sz, electronics2.plan(EGG, {**sz, "grip_target_n": 2.34}))
        w = {"id": "bench", "sha": f"t{len(_SIMS)}", "surface": SURFACE, "floor_z": -900.0, "obstacles": list(obstacles),
             "zones": []}
        rob = S._make_robot(asm, EGG, fast, {"world": w})
        plan, at = S._plan_pick_place(rob, EGG, fast)
        run, met = S._run_pick_place(rob, EGG, plan, at)
        _SIMS[key] = (rob, S._scenario_doc(rob, EGG, "pick_place", plan, run, met), met)
    return _SIMS[key]


def mujoco_clearance(rob, doc):
    """MuJoCo's clearance to the grey boxes (mj_geomDistance) at the poses scan_clearance checks."""
    dd = rob._mj.MjData(rob.model)
    out = []
    for _, q, o, ph in worlds._frame_poses(doc):
        dd.qpos[:] = 0
        dd.qpos[rob.qadr] = q[:rob.n]
        rob.set_grip(dd, q[-1])
        dd.qpos[rob.obj_q:rob.obj_q + 3] = o[:3] * S.MM
        dd.qpos[rob.obj_q + 3:rob.obj_q + 7] = S._q_body(o[3:7])
        rob._mj.mj_kinematics(rob.model, dd)
        out.append(rob.clearance(dd, with_object=ph in S.CARRIED)[0])
    return np.array(out)


# ------------------------------------------------------------------ the geometry
def test_closest_points_on_triangles():
    rng = np.random.default_rng(5)
    A, B, C = rng.normal(0, 30, (3, 400, 3))
    P = rng.normal(0, 60, (400, 3))
    Q = worlds.closest_on_triangles(P, A, B, C)
    ref = trimesh.triangles.closest_point(np.stack([A, B, C], 1), P)
    assert np.abs(np.linalg.norm(Q - P, axis=1) - np.linalg.norm(ref - P, axis=1)).max() < 1e-6
    # degenerate: two corners together (a segment), three in a row (a segment), all three together (a point)
    A2 = np.array([[0, 0, 0], [0, 0, 0], [5, 5, 5]], float)
    B2 = np.array([[10, 0, 0], [10, 0, 0], [5, 5, 5]], float)
    C2 = np.array([[10, 0, 0], [4, 0, 0], [5, 5, 5]], float)
    P2 = np.array([[5, 3, 0], [-4, 3, 0], [5, 5, 9]], float)
    assert np.allclose(worlds.closest_on_triangles(P2, A2, B2, C2), [[5, 0, 0], [0, 0, 0], [5, 5, 5]])


@pytest.mark.parametrize("h", [1.0, 2.0])
def test_the_scan_points_cover_every_triangle(h):
    """Every point of every triangle (slivers, segments and points too) is within rho of one of its own scan points:
    the bound both ways that the search relies on."""
    from scipy.spatial import cKDTree

    rng = np.random.default_rng(7)
    n = 160
    A = rng.normal(0, 20, (n, 3))
    B, C = A + rng.normal(0, 20, (n, 3)), A + rng.normal(0, 20, (n, 3))
    B[:30] = A[:30] + (B[:30] - A[:30]) * 10
    C[:30] = A[:30] + (B[:30] - A[:30]) * rng.uniform(0, 1, (30, 1)) + rng.normal(0, 0.05, (30, 3))
    C[30:36] = A[30:36]
    B[36:40] = C[36:40] = A[36:40]
    P, own, rho = worlds._scan_samples(A, B, C, h)
    assert worlds._scan_samples(A, B, C, h, count=True) == len(P) and 0 < rho <= h / math.sqrt(2) + 1e-9
    for i in range(n):
        X = np.vstack([rng.dirichlet([1, 1, 1], 400) @ np.stack([A[i], B[i], C[i]]), A[i], B[i], C[i]])
        assert cKDTree(P[own == i]).query(X)[0].max() <= rho + 1e-9, i


@pytest.mark.parametrize("which", ["bin", "carton-and-post"])
def test_within_2_mm_of_mujoco_on_box_fixtures(tmp_path, which):
    """Acceptance: a scan made of the world's own boxes, measured pose by pose (every 10 ms around the closest
    approach) against MuJoCo's mj_geomDistance to those boxes: within 2 mm everywhere (in practice a few tenths)."""
    boxes = ([bin_beside_the_arc(EGG, 20.0)] if which == "bin" else
             [carton_wall(EGG, 48.0), box("Post", (203.6, 83.6, 34.8), (20.7, 20.7, 34.8), 17.0)])
    rob, doc, met = simulated(tuple(boxes))
    t0 = met["min_clearance_at"]["t"]
    doc = {**doc, "frames": [f for f in doc["frames"] if abs(f["t"] - t0) < 0.6]}
    res = measure(boxes_glb(tmp_path / "room.glb", boxes), ROOM, doc, rob.xml, per_pose=True,
                                floor_cut_mm=0.0)
    mj = mujoco_clearance(rob, doc)
    sv = np.array([np.nan if v is None else v for _, v in res["poses"]])
    near = mj < 45.0
    assert near.sum() >= 40 and not np.isnan(sv[near]).any(), (near.sum(), res["stats"])
    diff = sv[near] - mj[near]
    print(f"\n{which}: {near.sum()} poses, scan - MuJoCo {diff.min():+.3f} to {diff.max():+.3f} mm, min {res['min_clearance_mm']} "
          f"vs {mj.min():.2f}, {res['stats']['seconds']} s")
    assert np.abs(diff).max() <= 2.0
    assert abs(res["min_clearance_mm"] - mj.min()) <= 2.0 and res["at"]["t"] == pytest.approx(t0, abs=0.6)
    assert not res["stats"]["truncated"] and res["note"] == worlds.SCAN_NOTE


# ------------------------------------------------------------------ the check, regions and Add box here
def test_add_box_here_turns_a_near_miss_into_a_box_the_simulation_keeps_clear_of(tmp_path):
    """The simulation sees no box, the scan has a post beside the carry path: the check warns of the near miss and
    proposes a box over it, on the bench; added to the world, the simulation goes round it and the near miss is gone."""
    room = boxes_glb(tmp_path / "room.glb", [SLAB, POST])
    rob, doc, met = simulated()
    assert met["success"] and met.get("min_clearance_mm") is None          # nothing for the simulation to see
    res = measure(room, ROOM, doc, rob.xml)
    assert res["min_clearance_mm"] < worlds.SCAN_NEAR_MM and len(res["regions"]) == 1, res
    g = res["regions"][0]
    assert g["id"] == "scan-1" and g["label"] == "Scan near miss 1" and g["min_clearance_mm"] == res["min_clearance_mm"]
    lo = np.subtract(g["box"]["center"], g["box"]["half"])
    hi = np.add(g["box"]["center"], g["box"]["half"])
    assert np.all(lo - 0.2 <= g["point"]) and np.all(np.array(g["point"]) <= hi + 0.2)      # over the near point
    assert abs(lo[2]) < 0.2 and hi[2] >= 69.0                               # standing on the bench, the post's height
    assert np.all(lo[:2] >= np.subtract(POST["center"], POST["half"])[:2] - 21)      # not much more than the post
    c = worlds.scan_check(res)
    assert c["status"] == "warn" and "Add box here" in c["text"] and c["text"].endswith(worlds.SCAN_NOTE)
    # Add box here: the region's box as a grey box, as the page adds it (validated like any saved world)
    w = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "asset": "room.glb",
                         "obstacles": [{"id": g["id"], "label": g["label"], **g["box"], "source": "auto"}]})
    ob = worlds.sim_world(w)["obstacles"]
    rob2, doc2, met2 = simulated(tuple(ob))
    assert met2["success"] and not met2["world_contacts"], met2.get("failure")
    assert met2["min_clearance_at"]["obstacle"] == "Scan near miss 1" and met2["min_clearance_mm"] >= S.CLEAR_MM
    assert met2["arc"]["usual"] is False                                    # the carry path pulled in to keep clear
    res2 = measure(room, ROOM, doc2, rob2.xml)
    assert res2["min_clearance_mm"] >= worlds.SCAN_NEAR_MM and not res2["regions"], res2
    assert worlds.scan_check(res2)["status"] == "pass"


def vase_mesh(c, r=22.0, h=70.0):
    v = trimesh.creation.cylinder(radius=r, height=h, sections=48)
    v.apply_translation([c[0], c[1], h / 2.0])
    return v


def test_add_box_here_never_comes_closer_to_the_path_than_the_scan_and_is_not_offered_again(tmp_path):
    """A round thing in the scan (a 44 mm vase) beside the carry path: a box drawn round it sticks out at its corners,
    towards the arm (the review's 9.0 mm near miss became 1.3 mm to the box). The box offered is trimmed on the sides
    facing the path until it is no closer to that path than the scan; once it is in the world, the same near miss is
    the box's and isn't offered again."""
    m = trimesh.util.concatenate([box_mesh(SLAB), vase_mesh(POST["center"][:2])])
    room = glb(tmp_path / "room.glb", m.vertices, m.faces)
    rob, doc, met = simulated()
    res = measure(room, ROOM, doc, rob.xml)
    assert res["min_clearance_mm"] < worlds.SCAN_NEAR_MM and len(res["regions"]) == 1, res
    g = res["regions"][0]
    obstacle = {"id": g["id"], "label": g["label"], **g["box"], "source": "auto"}
    w = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "asset": "room.glb", "obstacles": [obstacle]})
    rob_b, doc_b, met_b = simulated(tuple(worlds.sim_world(w)["obstacles"]))
    same_path = float(np.nanmin(mujoco_clearance(rob_b, doc)))             # MuJoCo: the box against the scanned path
    assert same_path >= g["min_clearance_mm"] - 1.0, (same_path, g)        # (MuJoCo's hulls and our samples: about 1 mm)
    assert g["trimmed"] and g["box_clearance_mm"] >= g["min_clearance_mm"] - 0.1, g
    lo, hi = np.subtract(g["box"]["center"], g["box"]["half"]), np.add(g["box"]["center"], g["box"]["half"])
    assert abs(lo[2]) < 0.2 and hi[2] >= 60.0 and np.all(hi[:2] - lo[:2] >= 20.0), g["box"]       # still most of the vase
    assert met_b["success"] and not met_b["world_contacts"]
    assert met_b["min_clearance_mm"] >= g["min_clearance_mm"] - 1.0, met_b["min_clearance_mm"]     # simulated with it
    # the same path, now with the box in the world: the near miss is still measured, but it is the box's
    res2 = measure(room, {**ROOM, "obstacles": [obstacle]}, doc, rob.xml)
    assert res2["min_clearance_mm"] == res["min_clearance_mm"] and not res2["regions"] and res2["stats"]["covered"] == 1
    c = worlds.scan_check(res2)
    assert c["status"] == "warn" and "Add box here" not in c["text"], c
    assert "1 place closer than 10 mm has a grey box already, so it is not offered again." in c["text"], c


TALL_VASE = ((POST["center"][0] + 40.0, POST["center"][1] - 20.0), 22.0, 170.0)   # its rim just under the carry path


def corners(b):
    return np.subtract(b["center"], b["half"]), np.add(b["center"], b["half"])


def test_a_box_trimmed_off_a_vases_top_still_covers_the_near_miss_it_was_made_for(tmp_path):
    """The review's case: the arm passes just over the rim of a tall vase. The box offered is trimmed off the top, so
    it ends up further from the near miss at the rim than SCAN_COVER_MM. It keeps the place it was made for: saved
    with it, that place is not offered again. The box alone (or the box moved elsewhere) does not cover it."""
    m = trimesh.util.concatenate([box_mesh(SLAB), vase_mesh(*TALL_VASE)])
    room = glb(tmp_path / "room.glb", m.vertices, m.faces)
    rob, doc, met = simulated()
    res = measure(room, ROOM, doc, rob.xml)
    (g,) = res["regions"]
    lo, hi = corners(g["box"])
    assert g["trimmed"] and hi[2] < g["point"][2] - worlds.SCAN_COVER_MM, g          # trimmed away from the near miss
    assert abs(lo[2]) < 0.2 and g["box_clearance_mm"] >= g["min_clearance_mm"] - 0.1, g
    clo, chi = corners(g["covers"])
    assert np.all(clo - 0.2 <= g["point"]) and np.all(np.array(g["point"]) <= chi + 0.2), g      # over the near miss
    assert chi[2] >= TALL_VASE[2] - 0.2 and np.all(clo <= lo + 0.05) and np.all(hi <= chi + 0.05), g
    obstacle = {"id": g["id"], "label": g["label"], **g["box"], "covers": g["covers"], "source": "auto"}
    w = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "asset": "room.glb",
                         "obstacles": [obstacle]})
    assert w["obstacles"][0]["covers"] == g["covers"] and worlds.sim_world(w)["obstacles"][0].keys() == {
        "id", "label", "center", "half", "yaw_deg"}                              # the simulation sees the box only
    res2 = measure(room, {**ROOM, "obstacles": w["obstacles"]}, doc, rob.xml)
    assert res2["min_clearance_mm"] == res["min_clearance_mm"] and not res2["regions"] and res2["stats"]["covered"] == 1
    c = worlds.scan_check(res2)
    assert c["status"] == "warn" and "Add box here" not in c["text"], c
    assert "1 place closer than 10 mm has a grey box already, so it is not offered again." in c["text"], c
    # the box alone is too far from the near miss to own it, and a box moved elsewhere doesn't cover the old place
    bare = {k: v for k, v in obstacle.items() if k != "covers"}
    moved = {**obstacle, "center": [obstacle["center"][0] + 200.0, *obstacle["center"][1:]]}
    for ob in (bare, moved):
        again = measure(room, {**ROOM, "obstacles": [ob]}, doc, rob.xml)
        assert [r["box"] for r in again["regions"]] == [g["box"]] and not again["stats"]["covered"], again


def test_a_box_not_fitted_in_time_is_not_offered(tmp_path, nominal, monkeypatch):
    """The boxes are fitted inside the check's time limit (the measuring passes leave them a share of it): a box that
    isn't fitted in time could come closer to the path than the scan, so none is offered, and the row says why."""
    rob, doc = nominal
    room = boxes_glb(tmp_path / "room.glb", [SLAB, POST])
    fit = worlds._fit_boxes
    monkeypatch.setattr(worlds, "_fit_boxes", lambda *a: fit(*a[:11], time.monotonic() - 1.0, a[12]))
    res = measure(room, ROOM, doc, rob.xml)
    (g,) = res["regions"]
    assert g["box"] is None and "ran out of time" in g["no_box"] and res["stats"]["fit_timeout"], g
    assert not {k for k in g if k.startswith("_")}
    assert "too close to the path for a box" in worlds.scan_check(res)["text"]


def test_the_scan_check_warns_and_never_fails():
    at = {"t": 4.25, "link": "jaw_left", "point": [1.0, 2.0, 3.0]}
    region = {"id": "scan-1", "label": "Scan near miss 1", "min_clearance_mm": 4.2, "box": {}}
    cases = [({"min_clearance_mm": 0.2, "at": at, "regions": [region]}, "warn", "The jaw left touches or passes through the scanned surface at 4.2 s."),
             ({"min_clearance_mm": 4.2, "at": at, "regions": [region]}, "warn", "The jaw left comes within 4.2 mm of the scanned surface at 4.2 s."),
             ({"min_clearance_mm": 4.2, "at": {**at, "link": "object"}, "regions": [region]}, "warn", "The object comes within"),
             ({"min_clearance_mm": 23.4, "at": at, "regions": []}, "pass", "At least 23 mm between the arm and the scanned surface (closest: the jaw left at 4.2 s)."),
             ({"min_clearance_mm": None, "at": None, "regions": []}, "pass", "The arm stays more than 50 mm from the scanned surface."),
             ({"min_clearance_mm": 23.4, "at": at, "regions": [], "stats": {"truncated": True}}, "warn", "Only part of the path was checked"),
             ({"min_clearance_mm": 23.4, "at": at, "regions": [], "stats": {"truncated": True}}, "warn",
              "At least 23 mm between the arm and the scanned surface in the part of the path checked (closest"),
             ({"min_clearance_mm": None, "at": None, "regions": [], "stats": {"truncated": True}}, "warn",
              "In the part of the path checked, the arm stays more than 50 mm from the scanned surface. Only part of the "
              "path was checked in time (a large scan or a busy computer): Check placement again to measure it all."),
             ({"skipped": "The room scan has no triangles."}, "skip", "The room scan has no triangles.")]
    for scan, status, text in cases:
        c = worlds.scan_check(scan)
        assert c["id"] == "scan" and c["status"] == status and text in c["text"], (c, text)
        assert c["text"].endswith(" " + worlds.SCAN_NOTE) and c["note"] == worlds.SCAN_NOTE
    assert "1 place closer than 10 mm: Add box here turns it into a grey box" in worlds.scan_check(cases[1][0])["text"]
    # inside the world check: a touching scan never changes the verdict, and the measured review advises on it
    w = worlds.starter("desk")
    out = worlds.checks(w, results(w), robot=ROBOT, design=design(), scan=cases[0][0])
    st = {c["id"]: c["status"] for c in out["checks"]}
    assert out["verdict"] == "ready" and st["scan"] == "warn" and "fail" not in st.values()
    assert out["checks"][[c["id"] for c in out["checks"]].index("clearance") + 1]["id"] == "scan"
    rv = worlds.rules_review(out["facts"], world=w)
    f = [x for x in rv["findings"] if "scanned surface" in x["evidence"]]
    assert f and f[0]["category"] == "clearance" and f[0]["recommendation"] == worlds._ADVICE["scan"]
    assert "scan" not in {c["id"] for c in worlds.checks(w, results(w), robot=ROBOT, design=design())["checks"]}


# ------------------------------------------------------------------ hostile scans
@pytest.fixture(scope="module")
def nominal():
    rob, doc, _ = simulated()
    return rob, doc


def post_mesh():
    return box_mesh(POST)


def test_hostile_files_are_reasons(tmp_path, nominal):
    rob, doc = nominal
    junk = tmp_path / "junk.glb"
    junk.write_bytes(os.urandom(2000))
    many = boxes_glb(tmp_path / "many.glb", [POST] * 10)                  # 120 triangles
    pts = trimesh.Scene()
    pts.add_geometry(trimesh.PointCloud(np.random.default_rng(1).normal(size=(50, 3))))
    (tmp_path / "points.glb").write_bytes(pts.export(file_type="glb"))
    for path, kw, why in ((junk, {}, "This isn't a .glb file"), (many, {"max_faces": 100}, "triangles"),
                          (tmp_path / "points.glb", {}, "no triangles"), (tmp_path / "missing.glb", {}, "couldn't be read")):
        res = measure(str(path), ROOM, doc, rob.xml, **kw)
        assert why in res["skipped"] and res["note"] == worlds.SCAN_NOTE, (path, res)
        assert worlds.scan_check(res)["status"] == "skip"
    ok = boxes_glb(tmp_path / "ok.glb", [POST])
    assert "collision model" in measure(ok, ROOM, doc, "<mujoco><bad")["skipped"]
    assert "no frames" in measure(ok, ROOM, {**doc, "frames": []}, rob.xml)["skipped"]
    assert "don't match" in measure(ok, ROOM, {**doc, "joints": ["no_such_joint"]}, rob.xml)["skipped"]


def test_nan_infinite_and_degenerate_corners_and_missing_ones(tmp_path, nominal, monkeypatch):
    """A post with NaN and infinite corners in some faces, faces naming corners that aren't there, slivers, segments
    and points: the bad faces are left out, the rest measured as the clean post is."""
    rob, doc = nominal
    clean = measure(boxes_glb(tmp_path / "clean.glb", [POST]), ROOM, doc, rob.xml)
    m = post_mesh()
    V, F = np.asarray(m.vertices, float), np.asarray(m.faces)
    n = len(V)
    V = np.vstack([V, [[np.nan, 0, 0], [np.inf, 1, 1], [-np.inf, 5, 5], [1e30, 0, 0]]])
    bad = [[n, 0, 1], [n + 1, 2, 3], [n + 2, n + 1, 4], [n + 3, 0, 1], [0, 0, 0], [1, 1, 2], [0, 1, 0]]
    path = glb(tmp_path / "hostile.glb", V, np.vstack([F, bad]))
    res = measure(path, ROOM, doc, rob.xml)
    assert res["min_clearance_mm"] == clean["min_clearance_mm"] and res["at"] == clean["at"], (res, clean)
    # faces that name corners the file doesn't have (a broken exporter): left out too
    real = trimesh.load(path, file_type="glb", force="mesh", skip_materials=True)
    broken = type("Mesh", (), {"vertices": np.asarray(real.vertices),
                               "faces": np.vstack([real.faces, [[0, 1, 10 ** 9], [-5, 0, 1]]])})()
    monkeypatch.setattr(trimesh, "load", lambda *a, **k: broken)
    assert measure(path, ROOM, doc, rob.xml)["min_clearance_mm"] == clean["min_clearance_mm"]


def test_gigantic_triangles_and_scans_far_away(tmp_path, nominal):
    """One triangle a kilometre across, standing across the arm's path, measures as a 2 m wall in the same place
    does (only the part near the arm is looked at); a scan 10 m away is nowhere near."""
    rob, doc = nominal
    doc = {**doc, "frames": [f for f in doc["frames"] if f.get("phase") == "approach"]}    # it passes closest then
    x = 280.0                                                   # a vertical plane x = 280 mm, in front of the robot

    def wall(size):
        return [[x, -size, -size / 2], [x, size, -size / 2], [x, 0.0, size]], [[0, 1, 2]]
    big = measure(glb(tmp_path / "big.glb", *wall(1e6)), ROOM, doc, rob.xml)
    small = measure(glb(tmp_path / "small.glb", *wall(2000.0)), ROOM, doc, rob.xml)
    assert big["min_clearance_mm"] is not None and abs(big["min_clearance_mm"] - small["min_clearance_mm"]) <= 0.1, (big, small)
    assert big["stats"]["triangles"] < 5000 and not big["stats"]["truncated"]
    far = measure(boxes_glb(tmp_path / "far.glb", [{**POST, "center": [10000.0, 0.0, 34.8]}]), ROOM, doc, rob.xml)
    assert far["min_clearance_mm"] is None and far["regions"] == [] and far["stats"]["triangles"] == 0
    assert worlds.scan_check(far)["status"] == "pass"


def test_the_first_pass_looks_along_the_whole_path_first():
    """Pass 1 visits the poses spread out (every 16th, then halfway between, ...), so a check cut short on a busy
    computer has looked along the whole path, not only its first seconds (the review's vase is passed at 3.2 s of 6.7)."""
    for n in (0, 1, 5, 16, 17, 100, 667):
        order = worlds._spread(n)
        assert sorted(order) == list(range(n)), n
    order = worlds._spread(667)
    assert order[:42] == list(range(0, 667, 16)) and max(order[:42]) >= 656     # the whole path in the first 42
    assert order[42:84] == list(range(8, 667, 16))


def test_out_of_time_says_so(tmp_path, nominal):
    rob, doc = nominal
    res = measure(boxes_glb(tmp_path / "room.glb", [SLAB, POST]), ROOM, doc, rob.xml, time_limit_s=0.0)
    assert res["stats"]["truncated"]
    c = worlds.scan_check(res)
    assert c["status"] in ("warn", "skip") and ("Only part of the path" in c["text"] or "too large" in c["text"])


# ------------------------------------------------------------------ a room of a million triangles
def million_room(path, target=1_000_000):
    """A generated room scan of about `target` triangles (y up, metres): a 4 x 4 m floor and four walls, finely
    gridded, a table whose top the robot stands on (z = 750 mm), and near the arm's path a mug, a box and a lamp tessellated
    as CAD exports do (long slivers), all with 1 mm of scan noise. The world placing the robot on the table."""
    def grid(nx, ny, sx, sy, T):
        x, y = np.meshgrid(np.linspace(-sx / 2, sx / 2, nx + 1), np.linspace(-sy / 2, sy / 2, ny + 1), indexing="ij")
        i = (np.arange(nx)[:, None] * (ny + 1) + np.arange(ny)[None, :]).ravel()
        m = trimesh.Trimesh(np.c_[x.ravel(), y.ravel(), np.zeros(x.size)],
                            np.r_[np.c_[i, i + ny + 1, i + 1], np.c_[i + 1, i + ny + 1, i + ny + 2]], process=False)
        m.apply_transform(T)
        return m

    def at(m, x, y, z, k=0):
        for _ in range(k):
            m = m.subdivide()
        m.apply_translation((x, y, z))
        return m

    tf = trimesh.transformations
    things = [at(trimesh.creation.box((1200, 700, 40)), 0, 0, 730, 6)]
    things += [at(trimesh.creation.box((40, 40, 710)), x, y, 355, 4) for x in (-560, 560) for y in (-310, 310)]
    things.append(at(trimesh.creation.cylinder(radius=40, height=95, sections=96), 205, 62, 797.5, 4))
    things.append(at(trimesh.creation.box((60, 60, 90)), 290, 160, 795, 5))
    things.append(at(trimesh.creation.cylinder(radius=10, height=400, sections=64), -250, 220, 950, 4))
    nf = int(math.sqrt((target - sum(len(t.faces) for t in things)) / 4.58))   # the floor 2 nf^2, the walls 4.8 nw^2
    nw = int(0.733 * nf)
    parts = things + [grid(nf, nf, 4000, 4000, np.eye(4))]
    for T in (tf.translation_matrix((0, 2000, 1250)) @ tf.rotation_matrix(math.pi / 2, (1, 0, 0)),
              tf.translation_matrix((0, -2000, 1250)) @ tf.rotation_matrix(math.pi / 2, (1, 0, 0)),
              tf.translation_matrix((2000, 0, 1250)) @ tf.rotation_matrix(math.pi / 2, (0, 1, 0)) @ tf.rotation_matrix(math.pi / 2, (1, 0, 0)),
              tf.translation_matrix((-2000, 0, 1250)) @ tf.rotation_matrix(math.pi / 2, (0, 1, 0)) @ tf.rotation_matrix(math.pi / 2, (1, 0, 0))):
        parts.append(grid(nw, int(nw * 0.6), 4000, 2500, T))
    m = trimesh.util.concatenate(parts)
    V = m.vertices + np.random.default_rng(0).normal(0, 1.0, m.vertices.shape)
    glb(path, V, m.faces)
    world = {"mount": {"position": [0.0, 0.0, 750.0], "yaw_deg": 0.0}, "units": "m", "up": "y", "scale": 1.0,
             "surface": {"label": "Table top", "center": [0.0, 0.0, 730.0], "half": [600.0, 350.0, 20.0], "yaw_deg": 0.0}}
    return len(m.faces), world


@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: a generated room of a million triangles (about a minute)")
def test_acceptance_a_million_triangle_room_in_under_20_s(tmp_path):
    faces, world = million_room(str(tmp_path / "room.glb"))
    assert faces >= 990_000
    rob, doc, met = simulated(fast=False)                     # the full simulation, as the Studio runs it
    t0 = time.time()
    res = worlds.scan_clearance(str(tmp_path / "room.glb"), world, doc, rob.xml)
    took = time.time() - t0
    st = res["stats"]
    print(f"\n{st['faces']} triangles, {st['kept_faces']} near the arm, {st['samples']} scan points (rho {st['rho_mm']} mm), "
          f"{st['poses']} poses: {took:.2f} s (load {st['load_s']}, crop {st['crop_s']}, tree {st['tree_s']}, pass 1 "
          f"{st['pass1_s']}, pass 2 {st['pass2_s']}, regions {st.get('regions_s')}, boxes {st.get('fit_s')}); closest "
          f"{res['min_clearance_mm']} mm at {res['at']}, "
          f"{len(res['regions'])} near-miss regions")
    assert st["faces"] == faces and not st["truncated"] and res.get("skipped") is None
    assert took <= 20.0 and res["min_clearance_mm"] is not None and res["regions"]


# ------------------------------------------------------------------ the Studio route
def scan_project(tmp_path, boxes):
    """r001 of an egg-picker project, simulated (fast) in a world whose room scan holds these boxes and whose grey
    boxes are none: the simulation can't see them."""
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import projects

    root = projects.create(str(tmp_path / "home"), "egg-picker", origin="default", demo=False, unique=False)["root"]
    os.makedirs(worlds.world_dir(root), exist_ok=True)
    path = boxes_glb(worlds.asset_path(root), boxes)
    w, w_sha = worlds.save(root, {"id": "bench", "name": "Bench scan", "asset": "room.glb", "asset_sha": worlds.glb_sha(path),
                                  "units": "m", "up": "y", "scale": 1.0, "mount": ROOM["mount"], "surface": SURFACE})
    d = EGG.with_changes({"world.id": w["id"], "world.sha": w_sha})
    rev = os.path.join(root, "r001")
    os.makedirs(os.path.join(rev, "sim"))
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    sw = RobotProject(root)._sim_world(d, os.path.join(rev, "sim"))
    sz = arm.size(d)
    doc = S.run(arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34})), d, "pick_place",
                os.path.join(rev, "sim"), fast=True, world=sw)
    res = {"rev": "r001", "kind": "robot", "sims": {"mjcf": "robot.xml", "scenarios": {
        "pick_place": {"file": "sim_pick_place.json", "metrics": doc["metrics"]}}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return root, w


def test_the_world_check_measures_the_path_against_the_scan(tmp_path):
    from morph.studio import api_world
    from test_world_api import Req, Room

    root, w = scan_project(tmp_path, [SLAB, POST])
    room = Room(root)
    api_world._SCANS.clear()
    st, body = api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
    assert st == 200, body
    c = {x["id"]: x for x in body["checks"]}
    assert c["scan"]["status"] == "warn" and c["scan"]["regions"][0]["label"] == "Scan near miss 1", c["scan"]
    assert c["scan"]["text"].endswith(worlds.SCAN_NOTE) and c["scan"]["value"] < worlds.SCAN_NEAR_MM
    assert c["clearance"]["status"] == "warn" and "no grey boxes" in c["clearance"]["text"]     # the simulation saw none
    out = worlds.checks(w, json.load(open(os.path.join(root, "r001", "results.json"), encoding="utf-8")),
                        sims=api_world._inputs(room, "r001")[1], design=room.p.load_design("r001"))
    assert body["verdict"] == out["verdict"]                                # the scan never changes the verdict
    scan_ev = [e for e in room.events if e["type"] == "world" and e["stage"] == "scan"]
    assert [e["status"] for e in scan_ev] == ["start", "done"] and "near miss" in scan_ev[1]["text"]
    assert any(e["type"] == "agent" and e["role"] == "feasibility" and e["text"] == scan_ev[1]["text"] for e in room.events)
    # checked again: the same answer, from memory
    room.events.clear()
    st, body2 = api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
    assert {x["id"]: x for x in body2["checks"]}["scan"] == c["scan"]
    assert not [e for e in room.events if e["type"] == "world" and e["stage"] == "scan"]
    # a check cut short (a busy computer) or a box not fitted in time isn't kept: checking again measures again
    for stats in ({"truncated": True}, {"fit_timeout": True}):
        api_world._SCANS.clear()
        calls = []
        real = worlds.scan_clearance
        worlds.scan_clearance = lambda *a, **k: calls.append(1) or {"min_clearance_mm": None, "at": None, "regions": [],
                                                                      "stats": stats, "note": worlds.SCAN_NOTE}
        try:
            for _ in range(2):
                api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
        finally:
            worlds.scan_clearance = real
        assert len(calls) == 2, stats
    # Reset demo reuses revision names: another simulation under the same name is measured again, never served from
    # memory (here the arm stays where it starts, well away from the post)
    sim = os.path.join(root, "r001", "sim", "sim_pick_place.json")
    with open(sim, encoding="utf-8") as fh:
        original = fh.read()
    moved = json.loads(original)
    f0 = moved["frames"][0]
    moved["frames"] = [{**f, "q": f0["q"], "obj": f0["obj"]} for f in moved["frames"]]
    with open(sim, "w", encoding="utf-8") as fh:
        json.dump(moved, fh)
    room.events.clear()
    st, body_r = api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
    assert [e["status"] for e in room.events if e["type"] == "world" and e["stage"] == "scan"] == ["start", "done"]
    s_r = {x["id"]: x for x in body_r["checks"]}["scan"]
    assert s_r["status"] == "pass" and "more than 50 mm" in s_r["text"] and not s_r["regions"]
    with open(sim, "w", encoding="utf-8") as fh:
        fh.write(original)
    # the robot moved in the room since the simulation: the old path isn't measured against the room
    worlds.save(root, {**w, "mount": {"position": [20.0, 0.0, 0.0], "yaw_deg": 0.0}})
    st, body3 = api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
    s3 = {x["id"]: x for x in body3["checks"]}["scan"]
    assert s3["status"] == "skip" and "Save and simulate" in s3["text"]
    # a world without a room scan has no scan check
    worlds.save(root, {**w, "asset": "", "asset_sha": ""})
    st, body4 = api_world.inspect_world(Req(room, body={"views": [], "ai": False}))
    assert "scan" not in {x["id"] for x in body4["checks"]}
