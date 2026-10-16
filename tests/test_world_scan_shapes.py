"""R6: the room-scan check measures the robot's own collision shapes and the motion between the poses it takes, so it
never claims more room than there is, and it counts, shares out and hands to grey boxes only what it measured.

The hunters' cases are kept as data (tests/data/scan_shape_cases.json) and built here on the egg-picker's nominal bench
path: small scanned things placed against the robot's real geometry (a plate under the carried egg, a speck under a
finger, a speck and a cable the jaw passes straight through between two 10 ms poses, plates along the path, a crafted
grey box). The reference is MuJoCo's own distance (mj_geomDistance) between each scanned box and each robot geom, at
the poses the check takes and every 0.25 ms of the moves around the closest one. Before R6 the check measured the
robot by points of its surface 1-2 mm apart (a capsule as a 24-sided prism) and only at the 10 ms poses, so it read
"At least 10 mm" for 9.95 mm and "comes within 0.8 mm" for a cable the jaw passes through."""

import json
import math
import os
import sys

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")
trimesh = pytest.importorskip("trimesh")

from morph.robot import simulate as S  # noqa: E402
from morph.studio import worlds  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_world_scan import ROOM, SURFACE, glb, simulated  # noqa: E402

HERE = os.path.dirname(__file__)
with open(os.path.join(HERE, "data", "scan_shape_cases.json"), encoding="utf-8") as _fh:
    CASES = json.load(_fh)


def rot(axis, deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    t = math.radians(deg)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * K + (1 - math.cos(t)) * K @ K


def scan_of(path, boxes):
    """A scan (.glb, as a phone exports it) made of boxes [(centre, half sizes, axes or None)] in the robot base frame
    (here the world frame: the robot stands at the origin of the bench top)."""
    parts = []
    for c, h, R in boxes:
        m = trimesh.creation.box(extents=2.0 * np.asarray(h, float))
        T = np.eye(4)
        T[:3, :3] = np.eye(3) if R is None else np.asarray(R, float)
        T[:3, 3] = c
        m.apply_transform(T)
        parts.append(m)
    m = trimesh.util.concatenate(parts)
    return glb(path, m.vertices, m.faces)


class Truth:
    """MuJoCo's exact distance between scanned boxes and the robot (the object only while it is carried, as the check
    takes it) at the check's poses, or at any pose along the path between them."""

    def __init__(self, rob, doc, boxes):
        spec = mujoco.MjSpec.from_string(rob.xml)
        for i, (c, h, R) in enumerate(boxes):
            g = spec.worldbody.add_geom()
            g.name, g.type = f"scan_{i}", mujoco.mjtGeom.mjGEOM_BOX
            g.size, g.pos = np.asarray(h, float) / 1000.0, np.asarray(c, float) / 1000.0
            if R is not None:
                q = np.zeros(4)
                mujoco.mju_mat2Quat(q, np.asarray(R, float).reshape(-1))
                g.quat = q
            g.contype = g.conaffinity = 0
        self.m = m = spec.compile()
        self.d = mujoco.MjData(m)
        self.scan = [m.geom(f"scan_{i}").id for i in range(len(boxes))]
        obj = m.body("sim_object").id
        self.obj_body = obj
        self.robot = [g for g in range(m.ngeom) if m.geom_bodyid[g] and m.body_rootid[m.geom_bodyid[g]] not in (0, obj)]
        self.obj = [g for g in range(m.ngeom) if m.geom_bodyid[g] == obj]
        self.names = list(doc["joints"])
        self.adr = {nm: (int(m.jnt_qposadr[m.joint(nm).id]),
                         1e-3 if m.jnt_type[m.joint(nm).id] == mujoco.mjtJoint.mjJNT_SLIDE else 1.0) for nm in self.names}
        self.mimic = [(int(m.jnt_qposadr[m.eq_obj1id[e]]), int(m.jnt_qposadr[m.eq_obj2id[e]]), np.asarray(m.eq_data[e][:5]))
                      for e in range(m.neq) if m.eq_type[e] == mujoco.mjtEq.mjEQ_JOINT]
        self.oq = int(m.jnt_qposadr[m.body_jntadr[obj]])
        self.poses = worlds._frame_poses(doc)

    def pose(self, p):
        m, d = self.m, self.d
        _, q, o, _ = p
        d.qpos[:] = m.qpos0
        for nm, v in zip(self.names, q):
            a, s = self.adr[nm]
            d.qpos[a] = v * s
        for a1, a2, c in self.mimic:
            x = d.qpos[a2]
            d.qpos[a1] = c[0] + x * (c[1] + x * (c[2] + x * (c[3] + x * c[4])))
        d.qpos[self.oq:self.oq + 3] = np.asarray(o[:3]) * 1e-3
        d.qpos[self.oq + 3:self.oq + 7] = S._q_body(np.asarray(o[3:7]))
        mujoco.mj_kinematics(m, d)

    def at(self, p, near=60.0):
        """(mm, geom name) at pose p: the closest of the scanned boxes to the robot (inf beyond `near`)."""
        self.pose(p)
        best, who, ft = math.inf, None, np.zeros(6)
        for g in self.robot + (self.obj if p[3] in S.CARRIED else []):
            for e in self.scan:
                v = 1000.0 * mujoco.mj_geomDistance(self.m, self.d, e, g, min(near, best) / 1000.0 + 1e-9, ft)
                if v < best:
                    best, who = v, self.m.geom(g).name
        return best, who

    def along(self):
        """(mm, pose index) over the check's poses."""
        v = [self.at(p)[0] for p in self.poses]
        k = int(np.argmin(v))
        return v[k], k

    def between(self, k0, k1, n=40):
        """The smallest distance over the moves k0..k1 posed n times each (every 0.25 ms of a 10 ms move)."""
        best = math.inf
        for k in range(max(0, k0), min(len(self.poses) - 1, k1)):
            (t0, q0, o0, ph), (t1, q1, o1, _) = self.poses[k], self.poses[k + 1]
            for u in np.linspace(0.0, 1.0, n + 1):
                o = o0 + (o1 - o0) * u
                o[3:] /= max(float(np.linalg.norm(o[3:])), 1e-9)
                best = min(best, self.at((t0 + (t1 - t0) * u, q0 + (q1 - q0) * u, o, ph))[0])
        return best

    def path(self, k=None, around=4):
        """The closest approach: over every pose, and every 0.25 ms of the moves around the closest pose (or pose k)."""
        v, k0 = self.along()
        k = k0 if k is None else k
        return min(v, self.between(k - around, k + around))


@pytest.fixture(scope="module")
def nominal():
    rob, doc, _ = simulated()
    return rob, doc


def measure(path, rob, doc, world=ROOM):
    res = worlds.scan_clearance(path, world, doc, rob.xml, time_limit_s=300.0, floor_cut_mm=0.0)
    return res, worlds.scan_check(res)


# ------------------------------------------------------------------ the hunters' configurations, built on this path
def geom_world(tr, g):
    m, d = tr.m, tr.d
    mid = m.geom_dataid[g]
    V = np.asarray(m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid] + m.mesh_vertnum[mid]], float) * 1000.0
    return V @ d.geom_xmat[g].reshape(3, 3).T + d.geom_xpos[g] * 1000.0


def at_distance(rob, doc, make, D, n_dir, steps=5):
    """Boxes make(shift) moved along n_dir until MuJoCo's closest approach over the poses is D (to 0.005 mm)."""
    shift = 0.0
    for _ in range(steps):
        boxes = make(shift)
        e = Truth(rob, doc, boxes).along()[0]
        if abs(e - D) < 0.005:
            break
        shift += e - D
    return boxes


def plate_under_object(rob, doc, D, phi):
    """A 1 mm plate under the carried egg (a capsule) where it is carried highest, its normal turned phi degrees about
    the egg's axis, D from the egg (the hunters' H1)."""
    tr = Truth(rob, doc, [((0.0, 0.0, 900.0), (1.0, 1.0, 1.0), None)])
    og = tr.obj[0]
    top = max((k for k, p in enumerate(tr.poses) if p[3] in S.CARRIED),
              key=lambda k: (tr.pose(tr.poses[k]), tr.d.geom_xpos[og][2])[1])
    tr.pose(tr.poses[top])
    c, ax = tr.d.geom_xpos[og] * 1000.0, tr.d.geom_xmat[og].reshape(3, 3)[:, 2]
    R = rot(ax, phi)
    n = R @ np.array([0.0, 0.0, 1.0])
    r = tr.m.geom_size[og][0] * 1000.0
    return at_distance(rob, doc, lambda s: [(c - n * (r + D + 0.5 - s), (10.0, 10.0, 0.5), R)], D, n)


def speck_under_finger(rob, doc, D):
    """A 0.1 mm speck D under the middle of the flat bottom of the left jaw's lowest finger part, at the bottom of the
    descent (the hunters' H1c): far from any of the old surface samples."""
    from scipy.spatial import ConvexHull

    tr = Truth(rob, doc, [((0.0, 0.0, 900.0), (1.0, 1.0, 1.0), None)])
    jaw = [g for g in tr.robot if tr.m.body(tr.m.geom_bodyid[g]).name == "jaw_left" and tr.m.geom_type[g] == 7]
    low = (math.inf, None, None)
    for k, p in enumerate(tr.poses):
        tr.pose(p)
        for g in jaw:
            z = geom_world(tr, g)[:, 2].min()
            if z < low[0]:
                low = (z, k, g)
    _, k, g = low
    tr.pose(tr.poses[k])
    V = geom_world(tr, g)
    h = ConvexHull(V)
    down = [i for i in range(len(h.simplices)) if h.equations[i, 2] < -0.9998]
    i = max(down, key=lambda i: np.linalg.norm(np.cross(*(V[h.simplices[i][1:]] - V[h.simplices[i][0]]))))
    p = V[h.simplices[i]].mean(0)
    return [(p - np.array([0.0, 0.0, D + 0.05]), (0.05, 0.05, 0.05), None)], k


def fastest_point(rob, doc):
    """The move the robot's fastest mesh corner covers most ground in (above the bench), its pose index, and that
    corner halfway along it with the centre of its geom: where a thing the arm passes straight through sits."""
    tr = Truth(rob, doc, [((0.0, 0.0, 900.0), (1.0, 1.0, 1.0), None)])
    mesh = [g for g in tr.robot if tr.m.geom_type[g] == 7]
    best = (0.0, None)
    prev = None
    for k, p in enumerate(tr.poses):
        tr.pose(p)
        now = {g: geom_world(tr, g) for g in mesh}
        if prev is not None:
            for g in mesh:
                dv = np.linalg.norm(now[g] - prev[g], axis=1)
                i = int(dv.argmax())
                if dv[i] > best[0] and min(now[g][i, 2], prev[g][i, 2]) > 15.0:
                    best = (float(dv[i]), (k - 1, g, i))
        prev = now
    k, g, i = best[1]
    (t0, q0, o0, ph), (t1, q1, o1, _) = tr.poses[k], tr.poses[k + 1]
    o = (o0 + o1) / 2.0
    o[3:] /= np.linalg.norm(o[3:])
    tr.pose(((t0 + t1) / 2.0, (q0 + q1) / 2.0, o, ph))
    return k, geom_world(tr, g)[i], tr.d.geom_xpos[g] * 1000.0, best[0]


def through(rob, doc, inside, half):
    k, v, centre, _ = fastest_point(rob, doc)
    s = v + (centre - v) / np.linalg.norm(centre - v) * inside      # just inside the geom there: the arm passes through
    return [(s, tuple(half), None)], k


def plates_along_path(rob, doc, count):
    """Small plates 5 mm outside the arm's outermost corner at poses spread along the path, more than 45 mm apart (each
    its own place), kept while MuJoCo puts them 1-9 mm from the arm (the hunters' R1)."""
    tr = Truth(rob, doc, [((0.0, 0.0, 900.0), (1.0, 1.0, 1.0), None)])
    mesh = [g for g in tr.robot if tr.m.geom_type[g] == 7]
    plates = []
    for k in range(0, len(tr.poses), 5):
        tr.pose(tr.poses[k])
        X = np.vstack([geom_world(tr, g) for g in mesh])
        X = X[X[:, 2] > 20.0]
        if not len(X):
            continue
        p = X[int(np.linalg.norm(X[:, :2], axis=1).argmax())]
        u = np.array([p[0], p[1], 0.0]) / np.linalg.norm(p[:2])
        c = (p + u * 5.5, (4.0, 4.0, 0.5), np.c_[np.cross([0.0, 0.0, 1.0], u), [0.0, 0.0, 1.0], u])
        if any(np.linalg.norm(c[0] - q[0]) < 45.0 for q in plates):
            continue
        if 1.0 < Truth(rob, doc, [c]).along()[0] < 9.0:
            plates.append(c)
        if len(plates) >= count:
            break
    return plates


def plate_over_arm(rob, doc, D):
    """A plate D over the robot's highest point anywhere along the path."""
    tr = Truth(rob, doc, [((0.0, 0.0, 900.0), (1.0, 1.0, 1.0), None)])
    mesh = [g for g in tr.robot if tr.m.geom_type[g] == 7]
    top = None
    for p in tr.poses:
        tr.pose(p)
        for g in mesh:
            X = geom_world(tr, g)
            i = int(X[:, 2].argmax())
            if top is None or X[i, 2] > top[2]:
                top = X[i].copy()
    z = np.array([0.0, 0.0, 1.0])
    return at_distance(rob, doc, lambda s: [(top + z * (D + 0.5 - s), (10.0, 10.0, 0.5), None)], D, z)


def offered(res, chk):
    return bool(res["regions"]) and not res["stats"].get("covered") and "Add box here" in chk["text"] and \
        "grey box already" not in chk["text"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_scan_check_never_claims_more_room_than_the_robots_real_shapes_leave(tmp_path, nominal, case):
    rob, doc = nominal
    kind, must = case["case"], case["must"]
    world, k_at = ROOM, None
    if kind == "plate_under_object":
        boxes = plate_under_object(rob, doc, case["D"], case["phi"])
    elif kind == "speck_under_finger":
        boxes, k_at = speck_under_finger(rob, doc, case["D"])
    elif kind == "speck_between":
        boxes, k_at = through(rob, doc, case["inside"], (0.05, 0.05, 0.05))
    elif kind == "cable_between":
        boxes, k_at = through(rob, doc, case["inside"], case["half"])
    elif kind == "plates_along_path":
        boxes = plates_along_path(rob, doc, case["count"])
        assert len(boxes) == case["count"], len(boxes)
    elif kind == "cabinet_covers":
        boxes = plate_over_arm(rob, doc, 6.0)
        c = np.asarray(boxes[0][0]) + np.array([0.0, 0.0, 0.5 + 45.0 + 50.0])     # a 100 mm Cabinet 45 mm over it
        cab = {"id": "cabinet", "label": "Cabinet", "center": [round(float(v), 1) for v in c], "half": [50.0] * 3,
               "yaw_deg": 0.0, "source": "user",
               "covers": {"center": [round(float(c[0]), 1), round(float(c[1]), 1), round(float(c[2]) - 25.0, 1)],
                          "half": [50.0, 50.0, 75.0], "yaw_deg": 0.0, "clearance_mm": case["clearance_mm"], "rev": "r001"}}
        world = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "asset": "room.glb",
                                 "units": "m", "up": "y", "scale": 1.0, "obstacles": [cab]})
    elif kind == "covered_closer":
        boxes = plate_under_object(rob, doc, case["D"], 9.0)
        res, _ = measure(scan_of(tmp_path / "first.glb", boxes), rob, doc)
        (g,) = res["regions"]
        assert g["trimmed"] and g["covers"]["clearance_mm"] == g["min_clearance_mm"], g
        up = lambda b: {**b, "center": [b["center"][0], b["center"][1], round(b["center"][2] + case["dz"], 1)]}  # noqa: E731
        ob = {"id": g["id"], "label": g["label"], **up(g["box"]), "covers": {**up(g["covers"]), "rev": "r001"},
              "source": "auto"}
        world = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "asset": "room.glb",
                                 "units": "m", "up": "y", "scale": 1.0, "obstacles": [ob]})
        boxes = [(np.asarray(c) + np.array([0.0, 0.0, case["dz"]]), h, R) for c, h, R in boxes]     # nearer the egg
    else:
        raise AssertionError(kind)
    tr = Truth(rob, doc, boxes)
    exact = tr.path(k_at)                               # MuJoCo: every pose, and every 0.25 ms around the closest
    res, chk = measure(scan_of(tmp_path / "room.glb", boxes), rob, doc, world)
    mc, text = res["min_clearance_mm"], chk["text"]
    print(f"\n{case['id']}: MuJoCo {exact:.3f} mm, check {mc}, {res['stats']['between_poses']} poses between, "
          f"{res['stats']['seconds']} s: {text[:160]}")
    assert not res["stats"]["truncated"], res["stats"]
    if exact < worlds.SCAN_TOUCH_MM:                    # it touches: the check says so, never how much room is left
        assert mc is not None and mc < worlds.SCAN_TOUCH_MM and "touches or passes through" in text, (mc, text)
        assert all(not r["exact"] for r in res["regions"]), res["regions"]          # a row there is "at most"
    else:
        assert mc is not None and mc <= exact + 1e-9, (case["id"], mc, exact)       # never more room than there is
        assert mc >= exact - 0.3, (case["id"], mc, exact)                           # and not much less
    if must == "near":
        assert chk["status"] == "warn" and "At least" not in text and f"comes within {worlds._mm1(mc)} mm" in text, text
    elif must == "touches":
        assert exact < worlds.SCAN_TOUCH_MM and "touches or passes through the scanned surface" in text, text
    elif must == "box_row":                             # the row's box, saved as Add box here saves it
        (g,) = res["regions"]
        ob = {"id": g["id"], "label": g["label"], **g["box"], "source": "auto"}
        w2 = worlds.validate({"id": "bench", "mount": ROOM["mount"], "surface": SURFACE, "obstacles": [ob]})
        b = worlds.box_to_base(w2, w2["obstacles"][0], nd=4)
        box_exact = Truth(rob, doc, [(b["center"], b["half"], worlds._rz(b["yaw_deg"]))]).path()
        assert g["box_clearance_mm"] is not None and g["box_clearance_mm"] <= box_exact + 1e-9, (g, box_exact)
        assert g["box_clearance_mm"] >= g["min_clearance_mm"] - 0.1 - 1e-9, g
    elif must == "count":                               # every place counted, the closest 8 listed
        assert len(res["regions"]) == worlds.SCAN_MAX_REGIONS and res["stats"]["places"] == case["count"], res["stats"]
        assert f"{case['count']} places closer than 10 mm (the {worlds.SCAN_MAX_REGIONS} closest are listed)" in text, text
    elif must == "offered":
        assert offered(res, chk), (res["stats"], text)
    elif must == "listed":                              # not the box's: offered again, or the box there is the one
        assert res["regions"] and not res["stats"].get("covered") and "grey box already" not in text, (res, text)
        assert "Add box here" in text or "the grey box there already is the one that fits" in text, text
    else:
        raise AssertionError(must)


@pytest.mark.parametrize("tol", [3.0, worlds.SCAN_BETWEEN_TOL_MM])
def test_the_distance_kept_is_a_lower_bound_of_the_whole_path_whatever_the_tolerance(tmp_path, nominal, monkeypatch, tol):
    """The third pass stops once no move could come closer than the closest found less SCAN_BETWEEN_TOL_MM, and the
    moves it left alone can still come up to that much closer. It kept the closest it measured: a speck 3 mm outside the
    fastest corner halfway along its move (MuJoCo: 2.75 mm along the path, 3.10 at the poses) read "comes within 2.9 mm"
    with a 3 mm tolerance, and with 0.1 mm a closest measured at 10.03 mm could read "At least 10 mm" over a move
    bounded at 9.95. The distance kept is a lower bound of the whole path: the closest measured, the moves still queued
    and the bounds of those left alone."""
    rob, doc = nominal
    boxes, k = through(rob, doc, -3.0, (0.05, 0.05, 0.05))
    exact = Truth(rob, doc, boxes).path(k)
    monkeypatch.setattr(worlds, "SCAN_BETWEEN_TOL_MM", tol)
    res, chk = measure(scan_of(tmp_path / "room.glb", boxes), rob, doc)
    mc = res["min_clearance_mm"]
    assert not res["stats"]["truncated"] and mc is not None and mc <= exact + 1e-9, (tol, mc, exact, chk["text"])
    assert mc >= exact - tol - 0.2, (tol, mc, exact)
    for r in res["regions"]:
        assert r["min_clearance_mm"] <= exact + 1e-9 or not r["exact"], (r, exact)


@pytest.mark.parametrize("checked, unique, share", [(606, 607, "(99% of it)"), (607, 607, "(100% of it)"),
                                                    (599, 600, "(99% of it)"), (3, 600, "(less than 1% of it)"),
                                                    (6, 600, "(1% of it)")])
def test_the_share_of_the_path_checked_is_never_rounded_up(checked, unique, share):
    """A check cut short after 606 of 607 poses said it looked at "(100% of it)": the share is rounded down."""
    c = worlds.scan_check({"min_clearance_mm": None, "at": None, "regions": [],
                           "stats": {"truncated": True, "part_clear": True, "poses_checked": checked, "unique_poses": unique}})
    assert share in c["text"], c["text"]


def test_a_check_cut_short_says_its_part_stays_clear_only_between_its_poses_too():
    """Cut short, "In the part of the path checked, the arm stays more than 50 mm" was decided on each pose's bound
    alone; a geom 50.5 mm clear at a pose with a 3 mm move to the next can come within 49 mm between them. The
    bound less half the larger move next to it decides it."""
    half = np.array([[1.5, 0.0], [1.5, 0.0]])                    # [pose, geom]: geom 7 moves 3 mm, geom 9 is fixed
    gi = {7: 0, 9: 1}
    assert not worlds._part_clear([(0, {7: 50.5, 9: 80.0}), (1, {7: 60.0})], half, gi)
    assert worlds._part_clear([(0, {7: 51.5, 9: 50.0}), (1, {7: 60.0})], half, gi)
    assert not worlds._part_clear([], half, gi)


def test_a_check_cut_short_counts_the_places_it_listed():
    """Only the listed places are counted when that is all there is (no "places" kept: a check measured before R6)."""
    at = {"t": 1.0, "link": "wrist", "point": [0.0, 0.0, 0.0]}
    rows = [{"id": f"scan-{i}", "label": f"Scan near miss {i}", "min_clearance_mm": 5.0, "box": {}} for i in (1, 2)]
    assert "2 places closer than 10 mm: Add box here" in worlds.scan_check({"min_clearance_mm": 5.0, "at": at,
                                                                            "regions": rows})["text"]
    text = worlds.scan_check({"min_clearance_mm": 5.0, "at": at, "regions": rows, "stats": {"places": 11}})["text"]
    assert "11 places closer than 10 mm (the 2 closest are listed): Add box here turns one" in text, text


def test_a_place_closer_than_its_box_was_fitted_for_is_listed_not_hidden():
    """The hunters' covered-closer case: a box Add box here made for a plate 2.0 mm from the path (its place fitted to
    1.9 mm), then the plate and the box 0.3 mm nearer the path. The box fitted to the new path is the same box, so it
    was taken out as "has a grey box already" while the path came within 1.6 mm of the scan. It is listed instead, without
    a box, saying the box there is the one that fits and the path comes closer than it was fitted to."""
    box = {"center": [0.0, 0.0, 50.0], "half": [20.0, 20.0, 50.0], "yaw_deg": 0.0}
    ob = {"id": "scan-1", "label": "Scan near miss 1", **box, "source": "auto",
          "covers": {"center": [0.0, 0.0, 65.0], "half": [20.0, 20.0, 65.0], "yaw_deg": 0.0, "clearance_mm": 1.9}}
    world = {"obstacles": [ob]}
    for mc, kept in ((1.6, True), (1.8, False), (1.9, False)):
        stats = {"places": 1}
        rows = worlds._already_boxed(world, [{"id": "scan-1", "label": "Scan near miss 1", "min_clearance_mm": mc,
                                              "box": dict(box)}], stats)
        assert bool(rows) == kept and bool(stats.get("covered")) != kept, (mc, rows, stats)
        if kept:
            (r,) = rows
            assert r["box"] is None and r["boxed"] == "Scan near miss 1" and "closer than the 1.9 mm" in r["no_box"], r
            text = worlds.scan_check({"min_clearance_mm": mc, "at": {"t": 1.0, "link": "object", "point": [0, 0, 0]},
                                      "regions": rows, "stats": stats})["text"]
            assert "the grey box there already is the one that fits" in text and "grey box already, so" not in text, text
    plain = {"obstacles": [{k: v for k, v in ob.items() if k != "covers"}]}      # no fit to go by: the box's, as before
    stats = {"places": 1}
    assert not worlds._already_boxed(plain, [{"min_clearance_mm": 1.6, "box": dict(box)}], stats) and stats["covered"] == 1
