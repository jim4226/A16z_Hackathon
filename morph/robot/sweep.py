"""How far a robot's geoms can travel between two poses, and the smallest distance along a path posed at discrete times.

A path is posed at times (the simulated frames, the plan's 10 ms samples, the scan check's poses) and moves between them
with every joint and the carried object linearly (the object's quaternion normalised in between), the way the
simulation's frames and the scan check pose it. `Motion.bound(a, b)` bounds the path length of every point of each geom
over such a move, from the joints that move it: a point at distance r from a hinge's axis travels at most r times the
hinge's turn, and the distances themselves grow at most by what the joints further out move (Grönwall), so
L = S (e^Q - 1) / Q with S the sum over the chain's joints of their turn times (the distance from their anchor to the
geom's centre plus its bounding radius) plus their slide, and Q the sum of the turns. A point that travels at most L
between two poses is, anywhere in between, within L/2 of where it was at one of them, so a geom no closer than D_a and
D_b to something at the two poses is no closer than (D_a + D_b - L) / 2 in between. `path_min` splits the moves whose
bound could be below the closest distance found until none could be closer than it by more than a tolerance.
"""

from __future__ import annotations

import heapq
import itertools
import math

import numpy as np


def _quat_angle(a, b) -> np.ndarray:
    """The turn (rad) between unit quaternions (rows of a and b)."""
    a = a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-12)
    b = b / np.maximum(np.linalg.norm(b, axis=-1, keepdims=True), 1e-12)
    return 2.0 * np.arccos(np.clip(np.abs((a * b).sum(-1)), 0.0, 1.0))


class Motion:
    """Path-length bounds for some geoms of a MuJoCo model (mm): `state(d)` after kinematics, `bound(a, b)` for the
    move between two states, one bound per geom (in the order given). Geoms on one body move together, so the bounds
    are worked out per body: its origin and a radius around it that holds its geoms (their offset plus their bounding
    radius)."""

    def __init__(self, m, geoms):
        import mujoco

        self.geoms = np.asarray(geoms, int)
        bodies, self.of = np.unique(np.asarray(m.geom_bodyid[self.geoms], int), return_inverse=True)
        self.bodies = bodies
        rb = np.zeros(len(bodies))
        for i, g in enumerate(self.geoms):
            r = float(np.linalg.norm(m.geom_pos[g])) + float(m.geom_rbound[g])
            rb[self.of[i]] = max(rb[self.of[i]], r)
        self.rb = rb * 1000.0
        chain = np.zeros((len(bodies), m.njnt), bool)
        for i, b in enumerate(bodies):
            b = int(b)
            while b > 0:
                a = int(m.body_jntadr[b])
                if a >= 0:
                    chain[i, a:a + int(m.body_jntnum[b])] = True
                b = int(m.body_parentid[b])
        self.jnt = np.nonzero(chain.any(0))[0]
        self.chain = chain[:, self.jnt].astype(float)
        typ = np.asarray(m.jnt_type[self.jnt])
        adr = np.asarray(m.jnt_qposadr[self.jnt])
        T = mujoco.mjtJoint
        self.hinge = adr[typ == T.mjJNT_HINGE], np.nonzero(typ == T.mjJNT_HINGE)[0]
        self.slide = adr[typ == T.mjJNT_SLIDE], np.nonzero(typ == T.mjJNT_SLIDE)[0]
        self.ball = adr[typ == T.mjJNT_BALL], np.nonzero(typ == T.mjJNT_BALL)[0]
        self.free = adr[typ == T.mjJNT_FREE], np.nonzero(typ == T.mjJNT_FREE)[0]

    def state(self, d):
        """What a bound needs of a pose (kinematics done): the joint positions and, for each body and joint of its
        chain, how far from the joint's anchor its geoms reach (mm)."""
        anchor = d.xanchor[self.jnt] * 1000.0
        origin = d.xpos[self.bodies] * 1000.0
        reach = np.sqrt(((origin[:, None, :] - anchor[None, :, :]) ** 2).sum(-1)) + self.rb[:, None]
        return d.qpos.copy(), self.chain * reach

    def bound(self, a, b) -> np.ndarray:
        """Each geom's path-length bound (mm) for the linear move from state a to state b."""
        qa, reach = a
        qb = b[0]
        turn, slide = np.zeros(len(self.jnt)), np.zeros(len(self.jnt))
        adr, i = self.hinge
        turn[i] = np.abs(qb[adr] - qa[adr])
        adr, i = self.slide
        slide[i] = np.abs(qb[adr] - qa[adr]) * 1000.0
        adr, i = self.ball
        if len(i):
            turn[i] = _quat_angle(np.stack([qa[x:x + 4] for x in adr]), np.stack([qb[x:x + 4] for x in adr]))
        adr, i = self.free
        if len(i):
            slide[i] = np.linalg.norm(np.stack([qb[x:x + 3] - qa[x:x + 3] for x in adr]), axis=1) * 1000.0
            turn[i] = _quat_angle(np.stack([qa[x + 3:x + 7] for x in adr]), np.stack([qb[x + 3:x + 7] for x in adr]))
        S = reach @ turn + self.chain @ slide
        Q = self.chain @ turn
        grow = np.where(Q > 1e-9, np.expm1(np.minimum(Q, 50.0)) / np.maximum(Q, 1e-9), 1.0)
        return (S * grow)[self.of]


def lerp_pose(a: dict, b: dict, u: float) -> dict:
    """The pose a fraction u of the way from a to b: joints ("q") and the object ("obj": position mm, quaternion)
    linearly, the quaternion normalised; time too; the rest (phase, held) as a's."""
    out = dict(a)
    out["t"] = a["t"] + (b["t"] - a["t"]) * u
    out["q"] = np.asarray(a["q"], float) + (np.asarray(b["q"], float) - np.asarray(a["q"], float)) * u
    if a.get("obj") is not None and b.get("obj") is not None:
        o = np.asarray(a["obj"], float) + (np.asarray(b["obj"], float) - np.asarray(a["obj"], float)) * u
        o[3:] /= max(float(np.linalg.norm(o[3:])), 1e-9)
        out["obj"] = o
    return out


def path_min(poses: list, evaluate, motion_of, tol: float, max_evals: int = 4000, stop: float = -math.inf) -> tuple:
    """The smallest distance along a path posed at `poses` (dicts with "t", "q", optionally "obj" and "held"), moving
    linearly between them (lerp_pose): `evaluate(pose, held)` poses it, measures the distance (mm, exact at that pose)
    and returns (distance, info, state); `motion_of(state_a, state_b, held)` bounds how far any point of the measured
    geoms travels between two poses (Motion.bound, the largest). Between poses held as the earlier one is. A move whose
    bound (D_a + D_b - L) / 2 is below the closest distance found less `tol` is halved, and so on, at most `max_evals`
    extra poses. Returns (lo, best, info): `lo` a lower bound of the distance along the whole path (within `tol` of
    `best` unless max_evals ran out), `best` the closest distance measured and `info` what `evaluate` said of it. Once
    a measured distance is below `stop` it returns at once, with that distance as lo (the caller only needs to know)."""
    if not poses:
        return math.inf, math.inf, None
    cache = {}

    def ev(k, p, held):
        key = (k, held)
        if key not in cache:
            cache[key] = evaluate(p, held)
        return cache[key]

    vals = [ev(k, p, bool(p.get("held"))) for k, p in enumerate(poses)]
    i0 = min(range(len(vals)), key=lambda i: vals[i][0])
    best, info = vals[i0][0], vals[i0][1]
    if best < stop:
        return best, best, info
    seq = itertools.count()
    heap = []

    def push(pa, va, pb, vb, held):
        nonlocal best, info
        if vb[0] < best:                                # b measured as held while coming from a: a real distance too
            best, info = vb[0], vb[1]
        L = float(np.max(motion_of(va[2], vb[2], held), initial=0.0))
        lb = (va[0] + vb[0] - L) / 2.0
        if lb < min(va[0], vb[0]):                     # else nothing in between is closer than an end
            heapq.heappush(heap, (lb, next(seq), pa, va, pb, vb, held))

    for k in range(len(poses) - 1):
        a, b = poses[k], poses[k + 1]
        held = bool(a.get("held"))
        if np.array_equal(np.asarray(a["q"]), np.asarray(b["q"])) and \
                (a.get("obj") is None or np.array_equal(np.asarray(a["obj"]), np.asarray(b.get("obj")))):
            continue
        push(a, ev(k, a, held), b, ev(k + 1, b, held), held)
    evals = 0
    while heap and heap[0][0] < best - tol and evals < max_evals and best >= stop:
        _, _, a, va, b, vb, held = heapq.heappop(heap)
        mid = lerp_pose(a, b, 0.5)
        vm = evaluate(mid, held)
        evals += 1
        if vm[0] < best:
            best, info = vm[0], vm[1]
        push(a, va, mid, vm, held)
        push(mid, vm, b, vb, held)
    if best < stop:
        return best, best, info
    lo = min(best, min((h[0] for h in heap), default=math.inf))
    return lo, best, info
