"""Generic 2-layer PCB generator: netlist -> footprints -> placement -> routing -> pours -> DRC -> fab files.

Input is a `schematic.Circuit` (components with footprints, pins with nets and
placement hints). Output is a board dict in the same JSON format as
`morph/board.py` (size_mm, components[{ref,value,outline,pads[{name,x,y,net}]}],
traces[{net,layer,width,points}], pour, ratsnest, drc) so the Studio's PCB build
animation can replay it, extended with SMD pad shapes, vias, mounting holes and
silkscreen, plus Gerbers (RS-274X X2), Excellon drill files and JLCPCB BOM/CPL.

Router: grid A* (0.25 mm grid, 8 directions with 45-degree bends preferred,
bend and via penalties) on F.Cu/B.Cu with per-net-class clearance rasters, so
every committed trace keeps >= CLEAR to other nets by construction; a separate
geometric DRC (shapely) re-checks clearance, edge distance, annular rings and
per-net connectivity (union-find over pads, traces, vias and pours).
Coordinates: mm, origin bottom-left of the board, y up.
"""

from __future__ import annotations

import csv
import heapq
import io
import json
import math
import os
import zipfile
from array import array

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import split, unary_union

GRID = 0.25
CLEAR = 0.2            # copper-to-copper, mm (JLCPCB 2-layer minimum is 0.127)
MARGIN = 0.04          # rasterisation safety so steps between cell centres never undercut CLEAR
EDGE_CLEAR = 0.4       # copper to board edge
POUR_CLEAR = 0.3       # pour to other nets
VIA_D, VIA_DRILL = 0.6, 0.3
CLASSES = {"sig": 0.2, "pwr": 0.6, "hc": 2.0}
LAYERS = ("F.Cu", "B.Cu")
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
TURN_COST = {0: 0.0, 1: 0.35, 2: 2.0, 3: 8.0}
VIA_COST = 9.0


# ------------------------------------------------------------------ footprints
def _pad(num, x, y, w, h=None, shape="rect", drill=0.0, np_=False):
    h = w if h is None else h
    return {"num": str(num), "x": float(x), "y": float(y), "w": float(w), "h": float(h), "shape": shape,
            "drill": float(drill), "layers": ["F.Cu", "B.Cu"] if drill > 0 else ["F.Cu"], "np": np_}


def _two(pitch, w, h, body, court, front=(0, 1), height=0.6):
    return {"pads": [_pad(1, -pitch / 2, 0, w, h), _pad(2, pitch / 2, 0, w, h)], "body": body, "court": court,
            "front": front, "height": height}


def _th_row(n, pitch, d, drill, y=0.0, start=1, sq=True):
    x0 = -(n - 1) * pitch / 2
    return [_pad(start + i, x0 + i * pitch, y, d, d, "rect" if (i == 0 and sq) else "circle", drill) for i in range(n)]


def footprint(name: str) -> dict:
    if name in ("R0603", "C0603", "LED0603"):
        return _two(1.65, 0.8, 0.95, (-0.8, -0.4, 0.8, 0.4), (-1.5, -0.75, 1.5, 0.75))
    if name == "C0805":
        return _two(1.9, 1.0, 1.45, (-1.0, -0.62, 1.0, 0.62), (-1.75, -1.0, 1.75, 1.0), height=1.2)
    if name == "SOD123":
        return _two(3.3, 0.9, 1.2, (-1.35, -0.8, 1.35, 0.8), (-2.35, -1.0, 2.35, 1.0))
    if name == "SMA":
        return _two(4.0, 2.5, 1.8, (-2.2, -1.3, 2.2, 1.3), (-3.5, -1.65, 3.5, 1.65), height=2.2)
    if name == "SMB":
        return _two(4.3, 2.5, 2.3, (-2.2, -1.8, 2.2, 1.8), (-3.7, -2.2, 3.7, 2.2), height=2.3)
    if name == "FUSE_2410":
        return _two(5.1, 2.0, 2.8, (-3.05, -1.35, 3.05, 1.35), (-3.9, -1.8, 3.9, 1.8), height=2.6)
    if name == "L_4x4":
        return _two(2.9, 1.5, 3.6, (-2.0, -2.0, 2.0, 2.0), (-2.45, -2.3, 2.45, 2.3), height=3.0)
    if name == "SOT23":
        return {"pads": [_pad(1, -0.95, -1.1, 0.6, 1.1), _pad(2, 0.95, -1.1, 0.6, 1.1), _pad(3, 0, 1.1, 0.6, 1.1)],
                "body": (-1.45, -0.65, 1.45, 0.65), "court": (-1.7, -1.95, 1.7, 1.95), "front": (0, 1), "height": 1.1}
    if name == "SOT23_6":
        pads = [_pad(i + 1, x, -1.1, 0.6, 1.1) for i, x in enumerate((-0.95, 0.0, 0.95))]
        pads += [_pad(i + 4, x, 1.1, 0.6, 1.1) for i, x in enumerate((0.95, 0.0, -0.95))]
        return {"pads": pads, "body": (-1.45, -0.8, 1.45, 0.8), "court": (-1.7, -2.0, 1.7, 2.0), "front": (0, 1), "height": 1.1}
    if name == "SOT223":
        pads = [_pad(i + 1, x, -3.15, 1.0, 1.8) for i, x in enumerate((-2.3, 0.0, 2.3))] + [_pad(2, 0, 3.15, 3.5, 1.8)]
        return {"pads": pads, "body": (-3.25, -1.75, 3.25, 1.75), "court": (-3.6, -4.3, 3.6, 4.3), "front": (0, 1), "height": 1.7}
    if name == "TO252":
        pads = [_pad(1, -2.28, -4.0, 1.2, 2.2), _pad(3, 2.28, -4.0, 1.2, 2.2), _pad(2, 0, 2.1, 6.0, 5.8)]
        return {"pads": pads, "body": (-3.3, -2.6, 3.3, 3.5), "court": (-3.6, -5.35, 3.6, 5.3), "front": (0, 1), "height": 2.4}
    if name == "SSOP8":
        pads = [_pad(i + 1, -1.8, 0.975 - 0.65 * i, 1.1, 0.4) for i in range(4)]
        pads += [_pad(i + 5, 1.8, -0.975 + 0.65 * i, 1.1, 0.4) for i in range(4)]
        return {"pads": pads, "body": (-1.4, -1.5, 1.4, 1.5), "court": (-2.6, -1.75, 2.6, 1.75), "front": (0, 1), "height": 1.3}
    if name == "TSSOP28":
        pads = [_pad(i + 1, -2.9, 4.225 - 0.65 * i, 1.5, 0.4) for i in range(14)]
        pads += [_pad(i + 15, 2.9, -4.225 + 0.65 * i, 1.5, 0.4) for i in range(14)]
        return {"pads": pads, "body": (-2.2, -4.85, 2.2, 4.85), "court": (-3.9, -5.1, 3.9, 5.1), "front": (0, 1), "height": 1.2}
    if name == "ESP32S3_WROOM1":
        pads = [_pad(i + 1, -8.75, 5.26 - 1.27 * i, 1.5, 0.9) for i in range(14)]
        pads += [_pad(15 + i, -6.985 + 1.27 * i, -12.5, 0.9, 1.5) for i in range(12)]
        pads += [_pad(27 + i, 8.75, -11.25 + 1.27 * i, 1.5, 0.9) for i in range(14)]
        pads.append(_pad(41, -1.5, -2.46, 3.7, 3.7))
        return {"pads": pads, "body": (-9.0, -12.75, 9.0, 12.75), "court": (-9.75, -13.5, 9.75, 12.75), "front": (0, 1),
                "height": 3.1, "keepout": (-9.0, 6.4, 9.0, 12.75), "fcu_keepout": (-7.9, -11.6, 7.9, 6.4)}
    if name == "USBC_16P":
        sig = [("A1B12", -3.2, 0.6), ("A4B9", -2.4, 0.6), ("B8", -1.75, 0.3), ("A5", -1.25, 0.3), ("B7", -0.75, 0.3),
               ("A6", -0.25, 0.3), ("A7", 0.25, 0.3), ("B6", 0.75, 0.3), ("A8", 1.25, 0.3), ("B5", 1.75, 0.3),
               ("B4A9", 2.4, 0.6), ("B1A12", 3.2, 0.6)]
        pads = [_pad(n, x, 0.0, w, 1.15) for n, x, w in sig]
        pads += [_pad("SH", sx * 4.32, y, 1.0, 1.8, "oval", 0.7) for sx in (-1, 1) for y in (1.66, 5.84)]
        pads += [_pad("", sx * 2.89, 1.15, 0.65, 0.65, "circle", 0.65, np_=True) for sx in (-1, 1)]
        # pre-routed D+/D- crossover (same idea as the vendor 2-layer footprints): D- (B7-A7) joined on F.Cu by a
        # U below the row, D+ (A6-B6) bridged on B.Cu through a via inside the U and one beside it. The GND pins
        # tie straight into the shell legs (plated, both layers).
        pre = [{"pad": "B7", "layer": "F.Cu", "pts": [(-0.75, 0.0), (-0.75, -1.6), (-1.0, -1.85), (-1.0, -3.0), (0.5, -3.0),
                                                      (0.5, -1.1), (0.25, -0.85), (0.25, 0.0)]},
               {"pad": "A6", "layer": "F.Cu", "pts": [(-0.25, 0.0), (-0.25, -2.1)]},
               {"pad": "B6", "layer": "F.Cu", "pts": [(0.75, 0.0), (0.75, -0.75), (1.2, -1.2), (1.2, -1.9)]},
               {"pad": "A6", "layer": "B.Cu", "pts": [(-0.25, -2.1), (1.0, -2.1), (1.2, -1.9)]},
               {"pad": "A1B12", "layer": "F.Cu", "w": 0.4, "pts": [(-3.2, 0.0), (-3.85, 0.65), (-4.32, 1.12), (-4.32, 1.66)]},
               {"pad": "B1A12", "layer": "F.Cu", "w": 0.4, "pts": [(3.2, 0.0), (3.85, 0.65), (4.32, 1.12), (4.32, 1.66)]}]
        return {"pads": pads, "body": (-4.47, 0.75, 4.47, 8.1), "court": (-5.1, -3.3, 5.1, 8.1), "front": (0, 1),
                "height": 3.2, "overhang": 0.6, "fcu_keepout": (-3.4, 0.9, 3.4, 8.1),
                "ties": {"A6": "DP", "B6": "DP", "B7": "DM", "A7": "DM", "A1B12": "SH", "B1A12": "SH", "SH": "SH"},
                "pre": pre, "pre_vias": [("A6", -0.25, -2.1), ("A6", 1.2, -1.9)], "no_stubs": True}
    if name == "SW_SMD_5x5":
        pads = [_pad(1, -3.0, 1.85, 1.0, 0.8), _pad(1, 3.0, 1.85, 1.0, 0.8), _pad(2, -3.0, -1.85, 1.0, 0.8),
                _pad(2, 3.0, -1.85, 1.0, 0.8)]
        return {"pads": pads, "body": (-2.55, -2.55, 2.55, 2.55), "court": (-3.75, -2.8, 3.75, 2.8), "front": (0, 1), "height": 1.5}
    if name == "XT30PW":
        pads = [_pad(1, 0, 2.5, 2.6, 2.6, "rect", 1.4), _pad(2, 0, -2.5, 2.6, 2.6, "circle", 1.4)]
        return {"pads": pads, "body": (-8.0, -5.1, 2.6, 5.1), "court": (-8.0, -5.3, 2.8, 5.3), "front": (-1, 0),
                "height": 8.0, "overhang": 6.0}
    if name == "CONN_2P5_3":
        return {"pads": _th_row(3, 2.5, 1.7, 0.95), "body": (-4.95, -2.2, 4.95, 3.6), "court": (-5.2, -2.45, 5.2, 3.85),
                "front": (0, -1), "height": 6.0}
    if name == "JST_PH_2":
        return {"pads": _th_row(2, 2.0, 1.25, 0.75), "body": (-2.95, -1.7, 2.95, 2.8), "court": (-3.2, -1.95, 3.2, 3.05),
                "front": (0, -1), "height": 4.5}
    if name == "JST_XH_2":
        return {"pads": _th_row(2, 2.5, 1.7, 1.0), "body": (-3.7, -2.35, 3.7, 3.4), "court": (-3.95, -2.6, 3.95, 3.65),
                "front": (0, -1), "height": 7.0}
    if name == "JST_SH_4":
        pads = [_pad(i + 1, -1.5 + i, -1.3, 0.6, 1.55) for i in range(4)] + [_pad("MP", sx * 2.8, 1.2, 1.2, 1.8) for sx in (-1, 1)]
        return {"pads": pads, "body": (-3.0, -0.5, 3.0, 3.7), "court": (-3.6, -2.3, 3.6, 3.7), "front": (0, 1), "height": 2.9}
    if name == "CP_D10":
        return {"pads": [_pad(1, -2.5, 0, 2.0, 2.0, "rect", 1.0), _pad(2, 2.5, 0, 2.0, 2.0, "circle", 1.0)],
                "body": (-5.0, -5.0, 5.0, 5.0), "court": (-5.25, -5.25, 5.25, 5.25), "front": (0, 1), "height": 16.0, "round": 5.0}
    if name.startswith("HDR_3x"):
        n = int(name.split("x")[1])
        x0 = -(n - 1) * 2.54 / 2
        pads = []
        for k in range(n):
            for r, y in enumerate((2.54, 0.0, -2.54)):
                pads.append(_pad(3 * k + r + 1, x0 + k * 2.54, y, 1.7, 1.7, "rect" if (k == 0 and r == 0) else "circle", 1.0))
        return {"pads": pads, "body": (x0 - 1.27, -3.81, -x0 + 1.27, 3.81), "court": (x0 - 1.52, -4.06, -x0 + 1.52, 4.06),
                "front": (0, -1), "height": 8.5}
    if name == "MH_M3":
        return {"pads": [_pad("1", 0, 0, 3.2, 3.2, "circle", 3.2, np_=True)], "body": (-3.0, -3.0, 3.0, 3.0),
                "court": (-3.2, -3.2, 3.2, 3.2), "front": (0, 1), "height": 0.0, "round": 3.0}
    raise KeyError(f"unknown footprint {name}")


def _rot(x, y, deg):
    a = math.radians(deg)
    c, s = round(math.cos(a)), round(math.sin(a))
    return x * c - y * s, x * s + y * c


def _rect_rot(r, deg, cx=0.0, cy=0.0):
    pts = [_rot(x, y, deg) for x, y in ((r[0], r[1]), (r[2], r[3]))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs) + cx, min(ys) + cy, max(xs) + cx, max(ys) + cy)


# ------------------------------------------------------------------ placement

class Placed:
    def __init__(self, comp, fp, x, y, rot):
        self.comp, self.fp, self.x, self.y, self.rot = comp, fp, x, y, rot
        self.court = _rect_rot(fp["court"], rot, x, y)
        self.body = _rect_rot(fp["body"], rot, x, y)
        self.pads = []
        for p in fp["pads"]:
            px, py = _rot(p["x"], p["y"], rot)
            w, h = (p["h"], p["w"]) if rot % 180 else (p["w"], p["h"])
            pin = None
            if p["num"]:
                try:
                    pin = comp.pin(p["num"])
                except KeyError:
                    pin = None
            self.pads.append({**p, "x": px + x, "y": py + y, "w": w, "h": h, "pin": pin,
                              "net": pin.net if pin else None, "tie": fp.get("ties", {}).get(p["num"])})


def _label_w(c) -> float:
    return 0.87 * len(f"{c.ref} {c.value}") * 1.0


def _overlap(a, b, gap=0.0):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


class Placer:
    def __init__(self, circuit, W, H, mount_holes):
        self.c, self.W, self.H = circuit, W, H
        self.placed: dict[str, Placed] = {}
        self.rects = []          # (rect, ref, kind) occupied: courtyards + label bands
        self.keepouts = []       # (rect) antenna etc. (no parts, no copper)
        self.holes = mount_holes

    def _band(self, c, court, top_edge=False):
        lw = max(_label_w(c), court[2] - court[0])
        return (court[0], court[3] + 0.1, court[0] + lw, court[3] + 2.3)

    def fits(self, c, fp, x, y, rot, edge=None):
        court = _rect_rot(fp["court"], rot, x, y)
        m = 0.6
        W, H = self.W, self.H
        if edge is None:
            if court[0] < m or court[1] < m or court[2] > W - m or court[3] > H - m:
                return False
        else:   # edge parts may overhang their own edge only
            ov = fp.get("overhang", 0.0) + 0.01
            lim = {"left": (-ov, m, W - m, H - m), "right": (m, m, W + ov, H - m), "bottom": (m, -ov, W - m, H - m),
                   "top": (m, m, W - m, H + ov)}[edge]
            if court[0] < lim[0] or court[1] < lim[1] or court[2] > lim[2] or court[3] > lim[3]:
                return False
        band = None if edge == "top" else self._band(c, court)
        if band and (band[2] > W + 1.5 or band[3] > H - 0.2):
            return False
        for r, ref, kind in self.rects:
            if kind == "court":
                if _overlap(court, r, 0.25) or (band and _overlap(band, r, 0.05)):
                    return False
            elif _overlap(court, r, 0.05) or (band and _overlap(band, r, 0.05)):
                return False
        for k in self.keepouts:
            if _overlap(court, k, 0.3) or (band and _overlap(band, k, 0.0)):
                return False
        return True

    def commit(self, c, fp, x, y, rot, edge=None):
        p = Placed(c, fp, x, y, rot)
        self.placed[c.ref] = p
        self.rects.append((p.court, c.ref, "court"))
        if edge != "top":
            self.rects.append((self._band(c, p.court), c.ref, "band"))
        if "keepout" in fp:
            self.keepouts.append(_rect_rot(fp["keepout"], rot, x, y))
        return p

    def edge_pose(self, c, fp, edge, at):
        out = {"left": (-1, 0), "right": (1, 0), "bottom": (0, -1), "top": (0, 1)}[edge]
        rot = next(r for r in (0, 90, 180, 270) if tuple(round(v) for v in _rot(*fp["front"], r)) == out)
        court = _rect_rot(fp["court"], rot)
        ov = fp.get("overhang", 0.0)
        W, H = self.W, self.H
        if edge == "left":
            x = -court[0] - ov + (0.0 if ov else 0.8)
            return x, at * H, rot, "y"
        if edge == "right":
            return W - court[2] + ov - (0.0 if ov else 0.8), at * H, rot, "y"
        if edge == "bottom":
            return at * W, -court[1] - ov + (0.0 if ov else 0.8), rot, "x"
        return at * W, H - court[3] + ov, rot, "x"

    def spiral(self, c, fp, tx, ty, rots, edge=None, axis=None, cost=None, max_r=None):
        g = 0.5
        best = None
        first = None
        max_r = max_r or max(self.W, self.H)
        r = 0.0
        while r <= max_r:
            cands = []
            if r == 0:
                cands = [(0.0, 0.0)]
            elif axis == "x":
                cands = [(r, 0.0), (-r, 0.0)]
            elif axis == "y":
                cands = [(0.0, r), (0.0, -r)]
            else:
                n = max(8, int(2 * math.pi * r / g))
                cands = [(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)) for k in range(n)]
            for dx, dy in cands:
                x, y = round((tx + dx) / GRID) * GRID, round((ty + dy) / GRID) * GRID
                for rot in rots:
                    if self.fits(c, fp, x, y, rot, edge):
                        k = (cost(x, y, rot) if cost else 0.0) + math.hypot(x - tx, y - ty)
                        if best is None or k < best[0]:
                            best = (k, x, y, rot)
                            if first is None:
                                first = r
            if first is not None and r > first + 2.0:
                break
            r += g
        return best

    def connected_target(self, c):
        pts = []
        for p in c.pins:
            if not p.net or p.net == "GND":
                continue
            for ref, pl in self.placed.items():
                for pad in pl.pads:
                    if pad["net"] == p.net:
                        pts.append((pad["x"], pad["y"]))
        if not pts:
            return None
        return float(np.mean([q[0] for q in pts])), float(np.mean([q[1] for q in pts]))

    def wirelen(self, c, fp, x, y, rot):
        tmp = Placed(c, fp, x, y, rot)
        tot = 0.0
        for pad in tmp.pads:
            if not pad["net"] or pad["net"] == "GND":
                continue
            best = None
            for pl in self.placed.values():
                for q in pl.pads:
                    if q["net"] == pad["net"]:
                        d = abs(q["x"] - pad["x"]) + abs(q["y"] - pad["y"])
                        best = d if best is None else min(best, d)
            tot += best or 0.0
        return tot

    def run(self):
        comps = [c for c in self.c.comps if c.board and c.footprint]
        W, H = self.W, self.H
        for c in comps:     # mounting holes first
            if c.place.get("hole") is not None:
                i = c.place["hole"]
                x, y = self.holes[i]
                self.commit(c, footprint(c.footprint), x, y, 0)
        edge = sorted([c for c in comps if c.place.get("edge")], key=lambda c: (c.place.get("keepout") is None, c.place["at"]))
        for c in edge:
            fp = footprint(c.footprint)
            x, y, rot, axis = self.edge_pose(c, fp, c.place["edge"], c.place["at"])
            best = self.spiral(c, fp, x, y, [rot], edge=c.place["edge"], axis=axis)
            if best is None:
                raise RuntimeError(f"cannot place {c.ref} on the {c.place['edge']} edge")
            self.commit(c, fp, best[1], best[2], best[3], edge=c.place["edge"])
        anchored = [c for c in comps if c.place.get("at_xy")]
        for c in anchored:
            fp = footprint(c.footprint)
            fx, fy = c.place["at_xy"]
            best = self.spiral(c, fp, fx * W, fy * H, [c.place.get("rot", 0)])
            if best is None:
                raise RuntimeError(f"cannot place {c.ref}")
            self.commit(c, fp, best[1], best[2], best[3])
        rest = sorted([c for c in comps if c.ref not in self.placed], key=lambda c: c.place.get("prio", 5))
        guard = 0
        while rest and guard < 1000:
            guard += 1
            c = rest.pop(0)
            near = c.place.get("near")
            if near and near not in self.placed and any(r.ref == near for r in rest):
                rest.append(c)
                continue
            fp = footprint(c.footprint)
            tgt = None
            if near and near in self.placed:
                pl = self.placed[near]
                if c.place.get("pin"):
                    pad = next((q for q in pl.pads if q["num"] == c.place["pin"]), None)
                    tgt = (pad["x"], pad["y"]) if pad else (pl.x, pl.y)
                else:
                    side = c.place.get("side")
                    cx, cy = pl.x, pl.y
                    w2, h2 = (pl.court[2] - pl.court[0]) / 2, (pl.court[3] - pl.court[1]) / 2
                    tgt = {"right": (cx + w2 + 3, cy), "left": (cx - w2 - 3, cy), "below": (cx, cy - h2 - 3),
                           "above": (cx, cy + h2 + 3)}.get(side, (cx, cy))
            if tgt is None:
                tgt = self.connected_target(c) or (W / 2, H / 2)
            two = len(footprint(c.footprint)["pads"]) == 2
            rots = [0, 90] if two else [0, 90, 180, 270] if len(fp["pads"]) <= 6 else [0]
            best = self.spiral(c, fp, tgt[0], tgt[1], rots, cost=lambda x, y, r, c=c, fp=fp: 0.6 * self.wirelen(c, fp, x, y, r))
            if best is None:
                raise RuntimeError(f"cannot place {c.ref}: board too small")
            self.commit(c, fp, best[1], best[2], best[3])
        return self.placed


# ------------------------------------------------------------------ router

class Router:
    """Per-(layer, width-class) clearance rasters plus a record of every copper shape, so routed nets can be
    ripped up and the rasters rebuilt (rip-up and re-route)."""

    def __init__(self, W, H, nets: list):
        self.W, self.H = W, H
        self.nx, self.ny = int(round(W / GRID)) + 1, int(round(H / GRID)) + 1
        self.net_id = {n: i for i, n in enumerate(nets)}
        self.traces, self.vias = [], []
        self.shapes = []                 # (layers, shape, net, tag)
        xs = np.arange(self.nx) * GRID
        ys = np.arange(self.ny) * GRID
        self.X, self.Y = np.meshgrid(xs, ys, indexing="ij")
        self.novia = np.zeros((self.nx, self.ny), dtype=bool)
        self.holeblock = np.zeros((self.nx, self.ny), dtype=bool)
        self._reset()

    def _reset(self):
        W, H = self.W, self.H
        self.own = {(l, k): np.full((self.nx, self.ny), -1, dtype=np.int32) for l in (0, 1) for k in CLASSES}
        for (l, k), arr in self.own.items():   # board edge keep-out (all classes, both layers)
            r = EDGE_CLEAR + CLASSES[k] / 2 + MARGIN
            m = (self.X < r) | (self.Y < r) | (self.X > W - r) | (self.Y > H - r)
            arr[m] = -2

    def rebuild(self):
        self._reset()
        for layers, shape, net, tag in self.shapes:
            self._raster(layers, shape, net)
        self.holeblock = self.th_holeblock.copy() if hasattr(self, "th_holeblock") else np.zeros_like(self.novia)
        for v in self.vias:
            self.block_holes(v["x"], v["y"], v["drill"])

    def rip(self, net):
        """Remove the routed copper (tag 'route') of `net`; call rebuild() afterwards."""
        self.shapes = [sh for sh in self.shapes if not (sh[2] == net and sh[3] == "route")]
        self.traces = [t for t in self.traces if not (t["net"] == net and t.get("_tag") == "route")]
        self.vias = [v for v in self.vias if not (v["net"] == net and v.get("_tag") == "route")]

    def copper_cells(self, net, key=None, tag=None):
        out = set()
        for t in self.traces:
            if t["net"] == net and (key is None or t.get("_key") == key) and (tag is None or t.get("_tag") == tag):
                out.update(t["_cells"])
        for v in self.vias:
            if v["net"] == net and (key is None or v.get("_key") == key) and (tag is None or v.get("_tag") == tag):
                out.update({(0, v["_cell"]), (1, v["_cell"])})
        return out

    def relaxed(self, net):
        """A copy whose rasters ignore every other net's routed copper (to find who blocks `net`)."""
        rb = Router.__new__(Router)
        rb.__dict__.update({k: v for k, v in self.__dict__.items() if k not in ("own", "shapes", "traces", "vias")})
        rb.traces, rb.vias = [], []
        rb.shapes = [sh for sh in self.shapes if sh[3] != "route" or sh[2] == net]
        rb.rebuild()
        return rb

    def nid(self, net):
        if net not in self.net_id:
            self.net_id[net] = len(self.net_id)
        return self.net_id[net]

    def idx(self, i, j):
        return i * self.ny + j

    def ij(self, idx):
        return divmod(idx, self.ny)

    def mark(self, layers, shape, net, tag="fixed"):
        self.shapes.append((tuple(layers), shape, net, tag))
        self._raster(layers, shape, net)

    def _raster(self, layers, shape, net):
        """Mark cells where a trace of each class would come within CLEAR of this copper."""
        k = self.nid(net) if net is not None else -2
        kind = shape[0]
        for l in layers:
            for cls, w in CLASSES.items():
                R = CLEAR + w / 2 + MARGIN
                if kind == "seg":
                    _, x0, y0, x1, y1, hw = shape
                    bb = (min(x0, x1) - hw - R, min(y0, y1) - hw - R, max(x0, x1) + hw + R, max(y0, y1) + hw + R)
                elif kind == "rect":
                    _, cx, cy, hw, hh = shape
                    bb = (cx - hw - R, cy - hh - R, cx + hw + R, cy + hh + R)
                elif kind == "circle":
                    _, cx, cy, rr = shape
                    bb = (cx - rr - R, cy - rr - R, cx + rr + R, cy + rr + R)
                else:   # area keep-out (rect, no expansion beyond R)
                    _, x0, y0, x1, y1 = shape
                    bb = (x0 - R, y0 - R, x1 + R, y1 + R)
                i0, j0 = max(int(math.floor(bb[0] / GRID)), 0), max(int(math.floor(bb[1] / GRID)), 0)
                i1, j1 = min(int(math.ceil(bb[2] / GRID)) + 1, self.nx), min(int(math.ceil(bb[3] / GRID)) + 1, self.ny)
                if i0 >= i1 or j0 >= j1:
                    continue
                X, Y = self.X[i0:i1, j0:j1], self.Y[i0:i1, j0:j1]
                if kind == "seg":
                    dx, dy = x1 - x0, y1 - y0
                    L2 = dx * dx + dy * dy
                    t = np.clip(((X - x0) * dx + (Y - y0) * dy) / L2, 0, 1) if L2 > 0 else 0.0
                    d = np.hypot(X - (x0 + t * dx), Y - (y0 + t * dy)) - hw
                elif kind == "rect":
                    d = np.hypot(np.maximum(np.abs(X - cx) - hw, 0), np.maximum(np.abs(Y - cy) - hh, 0))
                elif kind == "circle":
                    d = np.hypot(X - cx, Y - cy) - rr
                else:
                    d = np.hypot(np.maximum(np.maximum(x0 - X, X - x1), 0), np.maximum(np.maximum(y0 - Y, Y - y1), 0))
                m = d < R
                sub = self.own[(l, cls)][i0:i1, j0:j1]
                if k == -2:
                    sub[m] = -2
                else:
                    free = m & (sub == -1)
                    other = m & (sub != -1) & (sub != k)
                    sub[free] = k
                    sub[other] = -2

    def pad_cells(self, pad):
        """Grid cells whose centre lies inside the pad copper (route terminals)."""
        hw, hh = pad["w"] / 2 - 0.02, pad["h"] / 2 - 0.02
        if pad["shape"] in ("circle", "oval"):
            hw = hh = min(hw, hh)
        i0, i1 = int(math.ceil((pad["x"] - hw) / GRID)), int(math.floor((pad["x"] + hw) / GRID))
        j0, j1 = int(math.ceil((pad["y"] - hh) / GRID)), int(math.floor((pad["y"] + hh) / GRID))
        out = []
        for i in range(max(i0, 1), min(i1, self.nx - 2) + 1):
            for j in range(max(j0, 1), min(j1, self.ny - 2) + 1):
                if pad["shape"] == "circle" and math.hypot(i * GRID - pad["x"], j * GRID - pad["y"]) > hw:
                    continue
                out.append(self.idx(i, j))
        if not out:
            i, j = int(round(pad["x"] / GRID)), int(round(pad["y"] / GRID))
            out.append(self.idx(i, j))
        return out

    def astar(self, net, cls, sources, targets, goal, layers=(0, 1), allow_via=True, window=None, goal_fn=None,
              max_cost=None, bias_b=1.3, max_exp=250000):
        """A* over (cell, layer); 8 directions, 45-degree bends cheap, 90 expensive, vias cost VIA_COST."""
        k = self.nid(net)
        n = self.nx * self.ny
        free = [None, None]
        for l in (0, 1):
            a = self.own[(l, cls)]
            free[l] = ((a == -1) | (a == k)).ravel().tobytes()
        via_ok = None
        if allow_via:
            va, vb = self.own[(0, "pwr")], self.own[(1, "pwr")]
            via_ok = (((va == -1) | (va == k)) & ((vb == -1) | (vb == k)) & ~self.novia & ~self.holeblock).ravel().tobytes()
        ny = self.ny
        if window:
            wi0, wj0, wi1, wj1 = window
        else:
            wi0, wj0, wi1, wj1 = 1, 1, self.nx - 2, ny - 2
        gx, gy = goal
        tset = set(targets) if targets else set()
        tcells = [set(), set()]
        for l, c in tset:
            tcells[l].add(c)
        steps = [(di * ny + dj, di, dj, 1.414 if di and dj else 1.0) for di, dj in DIRS]
        INF = 1e18
        best = array("d", [INF]) * (2 * n)
        prev = array("l", [-1]) * (2 * n)
        dirs = bytearray(b"\x08" * (2 * n))
        hw = 1.0 if goal_fn else 1.3
        pq = []
        for (l, c) in sources:
            if l not in layers:
                continue
            key = c * 2 + l
            best[key] = 0.0
            i, j = divmod(c, ny)
            dx, dy = abs(i - gx), abs(j - gy)
            h = 0.0 if goal_fn else (dx + dy - 0.586 * min(dx, dy))
            heapq.heappush(pq, (h * hw, 0.0, key))
        exp = 0
        pop, push = heapq.heappop, heapq.heappush
        while pq:
            f, g, key = pop(pq)
            if g > best[key]:
                continue
            c, l = key >> 1, key & 1
            exp += 1
            if exp > max_exp:
                return None
            if (goal_fn and goal_fn(c, l)) or c in tcells[l]:
                path = [(l, c)]
                while prev[key] >= 0:
                    key = prev[key]
                    path.append((key & 1, key >> 1))
                return path[::-1]
            if max_cost and g > max_cost:
                continue
            i, j = divmod(c, ny)
            mult = bias_b if l == 1 else 1.0
            fl = free[l]
            d = dirs[key]
            tl = tcells[l]
            for nd in range(8):
                off, di, dj, base = steps[nd]
                ni, nj = i + di, j + dj
                if ni < wi0 or nj < wj0 or ni > wi1 or nj > wj1:
                    continue
                nc = c + off
                if not fl[nc] and nc not in tl:
                    continue
                if di and dj and (not fl[c + di * ny] or not fl[c + dj]):
                    continue
                if d != 8:
                    t = nd - d
                    if t < 0:
                        t = -t
                    if t > 4:
                        t = 8 - t
                    if t == 4:
                        continue
                    ng = g + base * mult + TURN_COST[t]
                else:
                    ng = g + base * mult
                nkey = nc * 2 + l
                if ng < best[nkey]:
                    best[nkey] = ng
                    prev[nkey] = key
                    dirs[nkey] = nd
                    if goal_fn:
                        push(pq, (ng, ng, nkey))
                    else:
                        dx, dy = abs(ni - gx), abs(nj - gy)
                        push(pq, (ng + (dx + dy - 0.586 * (dx if dx < dy else dy)) * hw, ng, nkey))
            if via_ok is not None and via_ok[c] and len(layers) == 2:
                ol = 1 - l
                if free[ol][c]:
                    ng = g + VIA_COST
                    nkey = c * 2 + ol
                    if ng < best[nkey]:
                        best[nkey] = ng
                        prev[nkey] = key
                        dirs[nkey] = 8
                        dx, dy = abs(i - gx), abs(j - gy)
                        push(pq, (ng + (0 if goal_fn else (dx + dy - 0.586 * min(dx, dy)) * hw), ng, nkey))
        return None

    def commit_path(self, net, path, width, start_xy=None, end_xy=None, tag="route", key=None):
        """Turn a cell path into traces (+ vias at layer changes), mark the rasters, return the copper cells."""
        runs, cur = [], [path[0]]
        cells = set()
        for a, b in zip(path, path[1:]):
            if b[0] != a[0]:
                runs.append(cur)
                cur = [b]
                v = self.ij(b[1])
                self.add_via(net, v[0] * GRID, v[1] * GRID, tag=tag, key=key)
                cells.update({(0, b[1]), (1, b[1])})
            else:
                cur.append(b)
        runs.append(cur)
        for ri, run in enumerate(runs):
            pts = _simplify_ij([self.ij(c) for _, c in run])
            xy = [[round(i * GRID, 4), round(j * GRID, 4)] for i, j in pts]
            if ri == 0 and start_xy is not None:
                xy = [list(start_xy)] + xy
            if ri == len(runs) - 1 and end_xy is not None:
                xy = xy + [list(end_xy)]
            xy = _dedupe(xy)
            li = run[0][0]
            rc = {(l, c) for l, c in run}
            cells |= rc
            if len(xy) >= 2:
                self.traces.append({"net": net, "layer": LAYERS[li], "width": width, "points": xy, "_tag": tag,
                                    "_key": key, "_cells": rc})
                for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
                    self.mark([li], ("seg", x0, y0, x1, y1, width / 2), net, tag)
        return cells

    def block_holes(self, x, y, drill):
        """No new via whose drill would come within 0.3 mm (edge to edge) of this hole."""
        rr = drill / 2 + VIA_DRILL / 2 + 0.3
        i0, i1 = max(int(math.floor((x - rr) / GRID)), 0), min(int(math.ceil((x + rr) / GRID)) + 1, self.nx)
        j0, j1 = max(int(math.floor((y - rr) / GRID)), 0), min(int(math.ceil((y + rr) / GRID)) + 1, self.ny)
        if i0 < i1 and j0 < j1:
            m = np.hypot(self.X[i0:i1, j0:j1] - x, self.Y[i0:i1, j0:j1] - y) < rr
            self.holeblock[i0:i1, j0:j1] |= m

    def add_via(self, net, x, y, tag="route", key=None):
        self.block_holes(x, y, VIA_DRILL)
        c = self.idx(int(round(x / GRID)), int(round(y / GRID)))
        self.vias.append({"x": round(x, 4), "y": round(y, 4), "d": VIA_D, "drill": VIA_DRILL, "net": net, "_tag": tag,
                          "_key": key, "_cell": c})
        self.mark([0, 1], ("circle", x, y, VIA_D / 2), net, tag)


def _simplify_ij(pts):
    out = [pts[0]]
    for a, b in zip(pts[1:], pts[2:] + [None]):
        if b is None:
            out.append(a)
            continue
        d1 = (a[0] - out[-1][0], a[1] - out[-1][1])
        d2 = (b[0] - a[0], b[1] - a[1])
        if d1[0] * d2[1] - d1[1] * d2[0] != 0 or d1[0] * d2[0] + d1[1] * d2[1] < 0:
            out.append(a)
    return out


def _dedupe(xy):
    out = []
    for p in xy:
        if not out or abs(out[-1][0] - p[0]) > 1e-6 or abs(out[-1][1] - p[1]) > 1e-6:
            out.append(p)
    return out


# ------------------------------------------------------------------ board generation

def _pad_shape(p):
    if p["shape"] == "circle":
        return ("circle", p["x"], p["y"], p["w"] / 2)
    if p["shape"] == "oval":
        r = min(p["w"], p["h"]) / 2
        if p["w"] >= p["h"]:
            return ("seg", p["x"] - (p["w"] / 2 - r), p["y"], p["x"] + (p["w"] / 2 - r), p["y"], r)
        return ("seg", p["x"], p["y"] - (p["h"] / 2 - r), p["x"], p["y"] + (p["h"] / 2 - r), r)
    return ("rect", p["x"], p["y"], p["w"] / 2, p["h"] / 2)


def _pad_geom(p):
    s = _pad_shape(p)
    if s[0] == "circle":
        return Point(s[1], s[2]).buffer(s[3], quad_segs=12)
    if s[0] == "seg":
        return LineString([(s[1], s[2]), (s[3], s[4])]).buffer(s[5], quad_segs=12)
    return box(p["x"] - p["w"] / 2, p["y"] - p["h"] / 2, p["x"] + p["w"] / 2, p["y"] + p["h"] / 2)


def net_class(net: str, hc_nets=(), pwr_nets=()) -> str:
    if net in hc_nets:
        return "hc"
    if net in pwr_nets:
        return "pwr"
    return "sig"


def _centre(group):
    return (float(np.mean([p["x"] for p in group])), float(np.mean([p["y"] for p in group])))


def _gkey(p):
    return (p["ref"], p.get("tie") or p["num"])


def _seg_cells(r, pts):
    out = set()
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(int(math.hypot(x1 - x0, y1 - y0) / GRID), 1)
        for k in range(n + 1):
            out.add(r.idx(int(round((x0 + (x1 - x0) * k / n) / GRID)), int(round((y0 + (y1 - y0) * k / n) / GRID))))
    return out


def _pre_copper(r, placed):
    """Footprint-defined copper (e.g. the USB-C D+/D- crossover): committed before routing, nets from its pads."""
    for ref, pl in placed.items():
        fp = pl.fp
        nets = {p["num"]: p["net"] for p in pl.pads}
        ties = fp.get("ties", {})
        for t in fp.get("pre", []):
            net = nets.get(t["pad"])
            if not net:
                continue
            pts = [_rot(x, y, pl.rot) for x, y in t["pts"]]
            pts = [[round(x + pl.x, 4), round(y + pl.y, 4)] for x, y in pts]
            li = LAYERS.index(t["layer"])
            key = (ref, ties.get(t["pad"]) or t["pad"])
            w = t.get("w", CLASSES["sig"])
            r.traces.append({"net": net, "layer": t["layer"], "width": w, "points": pts, "_tag": "pre",
                             "_key": key, "_cells": {(li, c) for c in _seg_cells(r, pts)}})
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                r.mark([li], ("seg", x0, y0, x1, y1, w / 2), net, "pre")
        for num, x, y in fp.get("pre_vias", []):
            net = nets.get(num)
            if net:
                vx, vy = _rot(x, y, pl.rot)
                r.add_via(net, vx + pl.x, vy + pl.y, tag="pre", key=(ref, ties.get(num) or num))


def _escape_fanout(r, placed, hc_nets, pwr_nets):
    """Reserve a short private escape stub straight out of every used pad of ICs and fine-pitch connectors,
    so later nets cannot run across the pad row and wall it in."""
    for ref, pl in placed.items():
        smd = [p for p in pl.pads if not p["drill"] and not p["np"]]
        if len(smd) < 3 or pl.fp.get("no_stubs"):
            continue
        xs = sorted({round(p["x"], 3) for p in smd})
        ys = sorted({round(p["y"], 3) for p in smd})
        pitch = min([b - a for a, b in zip(xs, xs[1:])] + [b - a for a, b in zip(ys, ys[1:])] + [9.9])
        for p in smd:
            if not p["net"] or p["net"] == "GND" or (p["w"] >= 3.0 and p["h"] >= 3.0) or p.get("tie"):
                continue
            dx, dy = p["x"] - pl.x, p["y"] - pl.y
            if p["w"] > p["h"] * 1.15:
                horiz = True
            elif p["h"] > p["w"] * 1.15:
                horiz = False
            else:
                horiz = abs(dx) * (pl.court[3] - pl.court[1]) >= abs(dy) * (pl.court[2] - pl.court[0])
            if horiz:
                ux, uy, ext = (1 if dx > 0 else -1), 0, p["w"] / 2
            else:
                ux, uy, ext = 0, (1 if dy > 0 else -1), p["h"] / 2
            L = ext + (0.75 if pitch < 1.0 else 0.6)
            cls = net_class(p["net"], hc_nets, pwr_nets)
            w = 0.6 if cls in ("hc", "pwr") and min(p["w"], p["h"]) >= 0.65 else CLASSES["sig"]
            cls = "pwr" if w >= 0.6 else "sig"
            i1, j1 = int(round((p["x"] + ux * L) / GRID)), int(round((p["y"] + uy * L) / GRID))
            x1, y1 = i1 * GRID, j1 * GRID
            k = r.nid(p["net"])
            n = max(abs(int(round((x1 - p["x"]) / GRID))), abs(int(round((y1 - p["y"]) / GRID))), 1)
            ok = True
            for t in range(n + 1):
                xi, yj = p["x"] + (x1 - p["x"]) * t / n, p["y"] + (y1 - p["y"]) * t / n
                ci, cj = int(round(xi / GRID)), int(round(yj / GRID))
                if not (0 < ci < r.nx - 1 and 0 < cj < r.ny - 1):
                    ok = False
                    break
                inside = abs(xi - p["x"]) <= p["w"] / 2 and abs(yj - p["y"]) <= p["h"] / 2
                if r.own[(0, cls)][ci, cj] not in (-1, k) and not inside:
                    ok = False
                    break
            if ok:
                r.commit_path(p["net"], [(0, r.idx(i1, j1))], w, (round(p["x"], 4), round(p["y"], 4)), None,
                              tag="stub", key=_gkey(p))


def _gnd_fanout(r, pads, unrouted):
    """Every SMD GND pad gets a short stub to a via into the B.Cu plane (done before signal routing)."""
    k_g = r.nid("GND")
    for p in pads:
        if p["net"] != "GND" or p["np"] or p["drill"] or p.get("tie"):
            continue
        if p["w"] >= 3.0 and p["h"] >= 3.0:          # exposed pad: via-in-pad array (tented)
            for dx in (-0.9, 0.9):
                for dy in (-0.9, 0.9):
                    r.add_via("GND", round((p["x"] + dx) / GRID) * GRID, round((p["y"] + dy) / GRID) * GRID,
                              tag="gndfan", key=_gkey(p))
            continue
        reach = max(p["w"], p["h"]) / 2 + VIA_D / 2 + 0.2
        va, vb = r.own[(0, "pwr")], r.own[(1, "pwr")]
        flat = (((va == -1) | (va == k_g)) & ((vb == -1) | (vb == k_g)) & ~r.novia & ~r.holeblock).ravel()
        px, py = p["x"], p["y"]

        def goal_fn(c, l, px=px, py=py, reach=reach, flat=flat):
            if l != 0 or not flat[c]:
                return False
            i, j = divmod(c, r.ny)
            return math.hypot(i * GRID - px, j * GRID - py) >= reach

        src = [(0, c) for c in r.pad_cells(p)]
        path, width = None, CLASSES["sig"]
        for cl in (("pwr", "sig") if min(p["w"], p["h"]) >= 0.6 else ("sig",)):
            path = r.astar("GND", cl, src, None, (0, 0), layers=(0,), allow_via=False, goal_fn=goal_fn, max_cost=28,
                           max_exp=20000)
            if path:
                width = 0.5 if cl == "pwr" else CLASSES["sig"]
                break
        if path is None:
            unrouted.append({"net": "GND", "pins": [f"{p['ref']}.{p['num']}"], "note": "no room for a GND via"})
            continue
        r.commit_path("GND", path, width, (round(px, 4), round(py, 4)), None, tag="gndfan", key=_gkey(p))
        i, j = r.ij(path[-1][1])
        r.add_via("GND", i * GRID, j * GRID, tag="gndfan", key=_gkey(p))


class _Routing:
    def __init__(self, r, terms, hc_nets, pwr_nets):
        self.r, self.terms, self.hc, self.pwr = r, terms, hc_nets, pwr_nets

    def pad_cells(self, g):
        out = set()
        for p in g:
            for c in self.r.pad_cells(p):
                out.add((0, c))
                if p["drill"]:
                    out.add((1, c))
        return out

    def group_cells(self, net, gk, g):
        return self.pad_cells(g) | self.r.copper_cells(net, key=gk, tag=None) - self.r.copper_cells(net, tag="route")

    def entry_ok(self, g, cls, net):
        """Cells of group g a trace of class cls may end in: free in the raster, or deep enough inside a pad."""
        r, w = self.r, CLASSES[cls]
        k = r.nid(net)
        out = set()
        for p in g:
            for c in r.pad_cells(p):
                i, j = r.ij(c)
                x, y = i * GRID, j * GRID
                if p["shape"] == "circle":
                    deep = math.hypot(x - p["x"], y - p["y"]) <= p["w"] / 2 - w / 2 + 0.01
                else:
                    deep = abs(x - p["x"]) <= p["w"] / 2 - w / 2 + 0.01 and abs(y - p["y"]) <= p["h"] / 2 - w / 2 + 0.01
                for l in ((0, 1) if p["drill"] else (0,)):
                    if deep or r.own[(l, cls)][i, j] in (-1, k):
                        out.add((l, c))
        return out

    def route_net(self, net):
        r = self.r
        groups = list(self.terms.get(net, {}).items())
        if len(groups) < 2:
            return []
        cls0 = net_class(net, self.hc, self.pwr)
        k = r.nid(net)
        cen = np.mean([_centre(g) for _, g in groups], axis=0)
        groups.sort(key=lambda kg: math.hypot(_centre(kg[1])[0] - cen[0], _centre(kg[1])[1] - cen[1]))
        tree = set(self.group_cells(net, *groups[0]))
        done, todo, fails = [groups[0]], groups[1:], []
        while todo:
            arr = np.array([r.ij(c) for _, c in tree]) * GRID
            todo.sort(key=lambda kg: float(np.min(np.abs(arr[:, 0] - _centre(kg[1])[0]) + np.abs(arr[:, 1] - _centre(kg[1])[1]))))
            gk, g = todo.pop(0)
            m = max(min(p["w"], p["h"]) for p in g)
            cls = cls0
            if cls == "hc" and m < 1.0:
                cls = "pwr"
            if cls == "pwr" and m < 0.6:
                cls = "sig"
            path, cls = self.search(net, cls, tree, gk, g)
            if path is None:
                fails.append((net, gk))
                continue
            sxy = exy = None
            end_c = path[-1][1]
            for p in g:
                if end_c in r.pad_cells(p):
                    exy = (round(p["x"], 4), round(p["y"], 4))
                    break
            st_l, st_c = path[0]
            for _, gg in done:
                for p in gg:
                    if st_c in r.pad_cells(p) and (st_l == 0 or p["drill"]):
                        sxy = (round(p["x"], 4), round(p["y"], 4))
                        break
            tree |= r.commit_path(net, path, CLASSES[cls], sxy, exy, tag="route", key=net)
            tree |= self.group_cells(net, gk, g)
            done.append((gk, g))
        return fails

    def search(self, net, cls, tree, gk, g, router=None):
        r = router or self.r
        k = r.nid(net)
        gx, gy = _centre(g)
        for attempt_cls in ([cls, "sig"] if cls != "sig" else ["sig"]):
            need = {"hc": 1.0, "pwr": 0.6, "sig": 0.0}[attempt_cls]
            okf = [r.own[(0, attempt_cls)].ravel(), r.own[(1, attempt_cls)].ravel()]
            srcs = [(l, c) for l, c in tree if okf[l][c] in (-1, k)]
            for _, gg in self.terms.get(net, {}).items():
                for p in gg:
                    if min(p["w"], p["h"]) >= need:
                        srcs += [lc for lc in self.pad_cells([p]) if lc in tree]
            if not srcs:
                srcs = list(tree)
            tgt = self.entry_ok(g, attempt_cls, net) | (self.group_cells(net, gk, g) - self.pad_cells(g))
            if not tgt:
                tgt = self.group_cells(net, gk, g)
            allp = [r.ij(c) for _, c in srcs] + [r.ij(c) for _, c in tgt]
            pad_i = 32
            win = (max(min(a for a, _ in allp) - pad_i, 1), max(min(b for _, b in allp) - pad_i, 1),
                   min(max(a for a, _ in allp) + pad_i, r.nx - 2), min(max(b for _, b in allp) + pad_i, r.ny - 2))
            path = r.astar(net, attempt_cls, srcs, tgt, (gx / GRID, gy / GRID), window=win, max_exp=50000)
            if path is None:
                path = r.astar(net, attempt_cls, srcs, tgt, (gx / GRID, gy / GRID), max_exp=90000)
            if path is not None:
                return path, attempt_cls
        return None, cls

    def blockers(self, net, gk):
        """Nets whose routed copper sits on the path this connection would take on an otherwise empty board."""
        r = self.r
        rb = r.relaxed(net)
        groups = self.terms.get(net, {})
        g = groups[gk]
        tree = set()
        for ok, og in groups.items():
            if ok != gk:
                tree |= self.group_cells(net, ok, og)
        tree |= r.copper_cells(net, tag="route")
        cls = net_class(net, self.hc, self.pwr)
        path, cls = self.search(net, cls, tree, gk, g, router=rb)
        if path is None:
            return None
        w = CLASSES[cls]
        pts = [(r.ij(c)[0] * GRID, r.ij(c)[1] * GRID, l) for l, c in path]
        out = set()
        for t in r.traces:
            if t["net"] == net or t.get("_tag") != "route":
                continue
            li = LAYERS.index(t["layer"])
            lim = CLEAR + w / 2 + t["width"] / 2 + 0.05
            line = LineString(t["points"]) if len(t["points"]) > 1 else Point(t["points"][0])
            for x, y, l in pts:
                if l == li and line.distance(Point(x, y)) < lim:
                    out.add(t["net"])
                    break
        for v in r.vias:
            if v["net"] == net or v.get("_tag") != "route":
                continue
            for x, y, l in pts:
                if math.hypot(x - v["x"], y - v["y"]) < CLEAR + w / 2 + VIA_D / 2 + 0.05:
                    out.add(v["net"])
                    break
        return out

    def run(self, order, max_rounds=24):
        fails = []
        for net in order:
            fails += self.route_net(net)
        tries: dict = {}
        rounds = 0
        while fails and rounds < max_rounds:
            rounds += 1
            net, gk = fails.pop(0)
            tries[(net, gk)] = tries.get((net, gk), 0) + 1
            if tries[(net, gk)] > 3:
                fails.append((net, gk))
                if all(tries.get(f, 0) > 3 for f in fails):
                    break
                continue
            bl = self.blockers(net, gk)
            if os.environ.get("PCB_DEBUG"):
                print("RRR", net, gk, "blockers:", bl, "pending:", fails)
            if not bl:
                fails.append((net, gk))
                continue
            for b in bl | {net}:
                self.r.rip(b)
            self.r.rebuild()
            new = self.route_net(net)
            for b in sorted(bl):
                new += self.route_net(b)
            fails = [f for f in fails if f[0] not in bl and f[0] != net] + new
        return fails


def generate(circuit, W: float, H: float, hc_nets=(), pwr_nets=(), mount_holes=None, name="board") -> dict:
    """Place and route `circuit` (board parts only) on a W x H mm 2-layer board."""
    holes = mount_holes or [(3.5, 3.5), (W - 3.5, 3.5), (3.5, H - 3.5), (W - 3.5, H - 3.5)]
    placed = Placer(circuit, W, H, holes).run()
    net_names = sorted(circuit.nets(board_only=True))
    pads = []
    for ref, pl in placed.items():
        for p in pl.pads:
            p["ref"] = ref
            pads.append(p)
    r = Router(W, H, net_names)
    nc = 0
    for p in pads:
        if p["np"]:
            r.mark([0, 1], ("circle", p["x"], p["y"], p["drill"] / 2 + 0.2), None)
            continue
        net = p["net"]
        if net is None:
            nc += 1
            net = p["net_uid"] = f"~nc{nc}"
        r.mark([0, 1] if p["drill"] else [0], _pad_shape(p), net)
        if not p["drill"]:    # no via-in-pad (vias must sit clear of SMD pads: VIA_D/2 + CLEAR)
            ex = VIA_D / 2 + 0.05
            i0, i1 = int(math.floor((p["x"] - p["w"] / 2 - ex) / GRID)), int(math.ceil((p["x"] + p["w"] / 2 + ex) / GRID))
            j0, j1 = int(math.floor((p["y"] - p["h"] / 2 - ex) / GRID)), int(math.ceil((p["y"] + p["h"] / 2 + ex) / GRID))
            r.novia[max(i0, 0):max(i1 + 1, 0), max(j0, 0):max(j1 + 1, 0)] = True
    for pl in placed.values():   # antenna keep-out (both layers) + no F.Cu routing under module bodies
        fp = pl.fp
        if "keepout" in fp:
            k = _rect_rot(fp["keepout"], pl.rot, pl.x, pl.y)
            r.mark([0, 1], ("area", k[0], k[1], k[2], k[3]), None)
        if "fcu_keepout" in fp:
            k = _rect_rot(fp["fcu_keepout"], pl.rot, pl.x, pl.y)
            r.mark([0], ("area", k[0], k[1], k[2], k[3]), None)
    for p in pads:
        if p["drill"]:
            r.block_holes(p["x"], p["y"], p["drill"])
    r.th_holeblock = r.holeblock.copy()
    terms: dict = {}
    for p in pads:
        if p["net"] and not p["np"]:
            terms.setdefault(p["net"], {}).setdefault(_gkey(p), []).append(p)
    unrouted = []
    _pre_copper(r, placed)
    _escape_fanout(r, placed, hc_nets, pwr_nets)
    _gnd_fanout(r, pads, unrouted)
    span = {}
    for n in net_names:
        pts = [(p["x"], p["y"]) for p in pads if p["net"] == n]
        span[n] = (max(x for x, _ in pts) - min(x for x, _ in pts) + max(y for _, y in pts) - min(y for _, y in pts)) if pts else 0
    rank = {"hc": 0, "pwr": 1, "sig": 2}
    order = sorted([n for n in net_names if n != "GND"], key=lambda n: (rank[net_class(n, hc_nets, pwr_nets)], span[n]))
    rt = _Routing(r, terms, hc_nets, pwr_nets)
    fails = rt.run(order)
    for net, gk in fails:
        unrouted.append({"net": net, "pins": [f"{p['ref']}.{p['num']}" for p in terms[net][gk]]})
    # stitching vias on a 6 mm lattice where both planes have room
    k_g = r.nid("GND")
    hc0, hc1 = r.own[(0, "hc")], r.own[(1, "hc")]
    for x in np.arange(6.0, W - 3.0, 6.0):
        for y in np.arange(6.0, H - 3.0, 6.0):
            i, j = int(round(x / GRID)), int(round(y / GRID))
            ok = all(a[i0, j0] in (-1, k_g) for a in (hc0, hc1)
                     for i0 in (i - 2, i, i + 2) for j0 in (j - 2, j, j + 2) if 0 <= i0 < r.nx and 0 <= j0 < r.ny)
            if ok and not r.novia[i, j] and not r.holeblock[i, j]:
                r.add_via("GND", i * GRID, j * GRID, tag="stitch")
    board = _assemble(circuit, placed, pads, r, W, H, unrouted, name)
    for _ in range(3):      # tie any GND island (e.g. a module's exposed pad under traces) back to the main plane
        islands = _gnd_islands(board)
        if len(islands) < 2:
            break
        islands.sort(key=len, reverse=True)
        main = islands[0]
        tgt = set()
        for v in board["vias"]:
            if v["net"] == "GND" and ("v", v["x"], v["y"]) in main:
                c = r.idx(int(round(v["x"] / GRID)), int(round(v["y"] / GRID)))
                tgt |= {(0, c), (1, c)}
        for p in pads:
            if p["net"] == "GND" and p["drill"] and ("p", p["ref"], p["num"]) in main:
                tgt |= {(l, c) for c in r.pad_cells(p) for l in (0, 1)}
        fixed = False
        for isl in islands[1:]:
            src = set()
            for v in board["vias"]:
                if v["net"] == "GND" and ("v", v["x"], v["y"]) in isl:
                    c = r.idx(int(round(v["x"] / GRID)), int(round(v["y"] / GRID)))
                    src |= {(0, c), (1, c)}
            for p in pads:
                if p["net"] == "GND" and ("p", p["ref"], p["num"]) in isl:
                    src |= {(0, c) for c in r.pad_cells(p)} | ({(1, c) for c in r.pad_cells(p)} if p["drill"] else set())
            if not src or not tgt:
                continue
            gx = float(np.mean([r.ij(c)[0] for _, c in tgt]))
            gy = float(np.mean([r.ij(c)[1] for _, c in tgt]))
            path = r.astar("GND", "sig", list(src), tgt, (gx, gy), max_exp=120000)
            if path:
                r.commit_path("GND", path, CLASSES["sig"], tag="route", key="GND")
                fixed = True
        if not fixed:
            break
        board = _assemble(circuit, placed, pads, r, W, H, unrouted, name)
    return board


def _gnd_islands(board) -> list:
    """GND copper islands as sets of ('p', ref, pad) / ('v', x, y) keys (pads, vias, traces, pours)."""
    pours = board.get("_pour_geoms") or []
    items = [it for it in _copper_items(board) if it[0] == "GND"] + [("GND", p["layer"], p["geom"], "pour", None) for p in pours]
    n = len(items)
    par = list(range(n))

    def f(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    by_key: dict = {}
    for i, it in enumerate(items):
        if it[3] in ("pad", "via"):
            by_key.setdefault(it[4], []).append(i)
    for ids in by_key.values():
        for a in ids[1:]:
            par[f(a)] = f(ids[0])
    from shapely.strtree import STRtree
    for layer in LAYERS:
        idx = [i for i, it in enumerate(items) if it[1] == layer]
        tree = STRtree([items[i][2] for i in idx])
        for a_pos, i in enumerate(idx):
            for b in tree.query(items[i][2]):
                j = idx[int(b)]
                if j != i and f(i) != f(j) and items[i][2].distance(items[j][2]) < 1e-4:
                    par[f(i)] = f(j)
    vias = board["vias"]
    groups: dict = {}
    for i, it in enumerate(items):
        if it[3] == "pad":
            groups.setdefault(f(i), set()).add(("p", it[4][0], it[4][1]))
        elif it[3] == "via":
            v = vias[it[4][1]]
            groups.setdefault(f(i), set()).add(("v", v["x"], v["y"]))
    return [g for g in groups.values() if any(k[0] == "p" for k in g)]


def _copper_items(board):
    """[(net, layer, geom, kind)] for DRC / pours."""
    items = []
    for c in board["components"]:
        for p in c["pads"]:
            if p.get("np"):
                continue
            g = _pad_geom(p)
            for l in p["layers"]:
                items.append((p.get("net") or p.get("uid"), l, g, "pad", (c["ref"], p["name"])))
    for t in board["traces"]:
        g = LineString(t["points"]).buffer(t["width"] / 2, quad_segs=8) if len(t["points"]) > 1 else Point(t["points"][0]).buffer(t["width"] / 2)
        items.append((t["net"], t["layer"], g, "trace", None))
    for i, v in enumerate(board["vias"]):
        g = Point(v["x"], v["y"]).buffer(v["d"] / 2, quad_segs=12)
        for l in LAYERS:
            items.append((v["net"], l, g, "via", ("~via", i)))
    return items


def _pours(board, W, H):
    items = _copper_items(board)
    outline = box(0, 0, W, H).buffer(-0.5, join_style=2)
    out = []
    keep = []
    for c in board["components"]:
        for p in c["pads"]:
            if p.get("np"):
                keep.append(Point(p["x"], p["y"]).buffer(p["drill"] / 2 + 0.5))
    for k in board.get("keepouts", []):
        keep.append(box(*k))
    for layer in LAYERS:
        obst = [g.buffer(POUR_CLEAR, quad_segs=6) for n, l, g, kind, _ in items if l == layer and n != "GND"]
        fk = [box(*k) for k in board.get("fcu_keepouts", [])] if layer == "F.Cu" else []
        area = outline.difference(unary_union(obst + keep + fk))
        area = area.buffer(-0.12, join_style=1).buffer(0.12, join_style=1)   # drop slivers narrower than ~0.25 mm
        polys = [area] if isinstance(area, Polygon) else [g for g in getattr(area, "geoms", []) if isinstance(g, Polygon)]
        gnd = [g for n, l, g, kind, _ in items if l == layer and n == "GND"]
        gu = unary_union(gnd) if gnd else None
        for poly in polys:
            if poly.area < 2.0 or gu is None or not poly.intersects(gu):
                continue
            out.append({"layer": layer, "net": "GND", "geom": poly})
    return out


def _uf_connectivity(board, pours):
    items = _copper_items(board)
    for p in pours:
        items.append(("GND", p["layer"], p["geom"], "pour", None))
    parent = list(range(len(items)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    by_net: dict = {}
    for i, it in enumerate(items):
        by_net.setdefault(it[0], []).append(i)
    pad_key: dict = {}
    for i, it in enumerate(items):
        if it[3] in ("pad", "via"):
            pad_key.setdefault(it[4], []).append(i)
    for ids in pad_key.values():          # same pad on both layers / same pin number = internally connected
        for a in ids[1:]:
            parent[find(a)] = find(ids[0])
    for net, ids in by_net.items():
        for a_i, a in enumerate(ids):
            ga, la = items[a][2], items[a][1]
            for b in ids[a_i + 1:]:
                if items[b][1] != la or find(a) == find(b):
                    continue
                if ga.distance(items[b][2]) < 1e-4:
                    parent[find(a)] = find(b)
    bad = []
    for net, ids in by_net.items():
        if net is None or str(net).startswith("~"):
            continue
        pads = [i for i in ids if items[i][3] == "pad"]
        roots = {find(i) for i in pads}
        if len(roots) > 1:
            bad.append({"net": net, "islands": len(roots)})
    return bad


def drc(board, pours) -> dict:
    W, H = board["size_mm"]
    items = _copper_items(board) + [("GND", p["layer"], p["geom"], "pour", None) for p in pours]
    viol = []
    from shapely.strtree import STRtree
    for layer in LAYERS:
        li = [it for it in items if it[1] == layer]
        geoms = [it[2] for it in li]
        tree = STRtree(geoms)
        for a, it in enumerate(li):
            for b in tree.query(it[2].buffer(CLEAR)):
                b = int(b)
                if b <= a:
                    continue
                o = li[b]
                if o[0] == it[0] and o[0] is not None:
                    continue
                if it[4] is not None and it[4] == o[4]:
                    continue
                d = it[2].distance(o[2])
                lim = POUR_CLEAR - 0.02 if "pour" in (it[3], o[3]) else CLEAR - 0.005
                if d < lim:
                    x, y = it[2].centroid.x, it[2].centroid.y
                    viol.append({"rule": "clearance", "layer": layer, "nets": [it[0], o[0]], "dist_mm": round(d, 3),
                                 "at": [round(x, 2), round(y, 2)]})
    inner = box(0, 0, W, H).buffer(-EDGE_CLEAR + 0.01)
    over = {c["ref"] for c in board["components"] if c.get("overhang")}
    for it in items:
        if it[3] == "pad" and it[4] and it[4][0] in over:
            continue
        if not inner.contains(it[2]) and it[3] != "pour":
            viol.append({"rule": "edge_clearance", "net": it[0], "layer": it[1], "at": [round(it[2].centroid.x, 2), round(it[2].centroid.y, 2)]})
    rings = []
    for c in board["components"]:
        for p in c["pads"]:
            if p["drill"] and not p.get("np"):
                ring = (min(p["w"], p["h"]) - p["drill"]) / 2
                if ring < 0.13:
                    rings.append({"rule": "annular_ring", "pad": f"{c['ref']}.{p['name']}", "ring_mm": round(ring, 3)})
    for v in board["vias"]:
        if (v["d"] - v["drill"]) / 2 < 0.13:
            rings.append({"rule": "annular_ring", "via": [v["x"], v["y"]]})
    holes = [(p["x"], p["y"], p["drill"]) for c in board["components"] for p in c["pads"] if p["drill"]] + \
            [(v["x"], v["y"], v["drill"]) for v in board["vias"]]
    hh = []
    for i, a in enumerate(holes):
        for b in holes[i + 1:]:
            gap = math.hypot(a[0] - b[0], a[1] - b[1]) - a[2] / 2 - b[2] / 2
            if gap < 0.25 and gap > -1e-6 and not (abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6):
                hh.append({"rule": "hole_to_hole", "at": [a[0], a[1]], "gap_mm": round(gap, 3)})
    disconnected = _uf_connectivity(board, pours)
    min_w = min([t["width"] for t in board["traces"]] or [0.25])
    viol = viol + rings + hh
    return {"pass": not viol and not board["unrouted"] and not disconnected, "unrouted": board["unrouted"],
            "disconnected": disconnected, "violations": viol[:50], "n_violations": len(viol),
            "clearance_mm": CLEAR, "min_trace_mm": min_w, "via_mm": [VIA_D, VIA_DRILL], "edge_clearance_mm": EDGE_CLEAR,
            "checks": ["clearance", "edge_clearance", "annular_ring", "hole_to_hole", "connectivity", "unrouted"]}


def _assemble(circuit, placed, pads, r, W, H, unrouted, name):
    comps = []
    for ref, pl in placed.items():
        c = pl.comp
        ps = []
        for p in pl.pads:
            if p["np"] and c.kind != "MH":
                ps.append({"name": "", "num": "", "x": round(p["x"], 4), "y": round(p["y"], 4), "net": None, "w": p["w"],
                           "h": p["h"], "shape": "circle", "drill": p["drill"], "layers": [], "np": True})
                continue
            ps.append({"name": p["num"], "pin": p["pin"].name if p["pin"] else "", "x": round(p["x"], 4), "y": round(p["y"], 4),
                       "net": p["net"], "w": round(p["w"], 3), "h": round(p["h"], 3), "shape": p["shape"], "drill": p["drill"],
                       "layers": [] if p["np"] else p["layers"], "np": p["np"], **({"uid": p.get("net_uid")} if p.get("net_uid") else {})})
        comps.append({"ref": ref, "value": c.value, "footprint": c.footprint, "x": round(pl.x, 4), "y": round(pl.y, 4),
                      "rot": pl.rot, "side": "F", "outline": [round(v, 3) for v in pl.body], "court": [round(v, 3) for v in pl.court],
                      "pads": ps, "mpn": c.mpn, "lcsc": c.lcsc, "smd": not any(p["drill"] for p in pl.pads if not p["np"]),
                      "height": pl.fp.get("height", 1.0), "round": pl.fp.get("round"), "overhang": bool(pl.fp.get("overhang")),
                      "kind": c.kind})
    keepouts = [_rect_rot(pl.fp["keepout"], pl.rot, pl.x, pl.y) for pl in placed.values() if "keepout" in pl.fp]
    fk = [_rect_rot(pl.fp["fcu_keepout"], pl.rot, pl.x, pl.y) for pl in placed.values() if "fcu_keepout" in pl.fp]
    nets = {}
    for n, members in circuit.nets(board_only=True).items():
        nets[n] = [c.node(p) for c, p in members]
    # ratsnest: minimum spanning tree per net over pad centres
    pad_xy = {}
    for c in comps:
        for p in c["pads"]:
            if p.get("net"):
                pad_xy.setdefault(p["net"], []).append((p["x"], p["y"]))
    rats = []
    for n, pts in pad_xy.items():
        pts = list(dict.fromkeys(pts))
        if len(pts) < 2:
            continue
        inn, rest = [pts[0]], pts[1:]
        while rest:
            a, b = min(((a, b) for a in inn for b in rest), key=lambda ab: math.hypot(ab[0][0] - ab[1][0], ab[0][1] - ab[1][1]))
            rats.append({"net": n, "a": list(a), "b": list(b)})
            inn.append(b)
            rest.remove(b)
    traces = [{**{k: v for k, v in t.items() if not k.startswith("_")}, "kind": t.get("_tag", "route")} for t in r.traces]
    vias = [{**{k: v for k, v in v.items() if not k.startswith("_")}, "kind": v.get("_tag", "route")} for v in r.vias]
    board = {"name": name, "size_mm": [round(W, 2), round(H, 2)], "layers": 2, "thickness_mm": 1.6, "copper_oz": 1,
             "components": comps, "nets": nets, "traces": traces, "vias": vias, "ratsnest": rats,
             "keepouts": [list(map(float, k)) for k in keepouts], "fcu_keepouts": [list(map(float, k)) for k in fk],
             "unrouted": unrouted, "grid_mm": GRID}
    pours = _pours(board, W, H)
    board["pour"] = []
    for p in pours:
        g = p["geom"]
        board["pour"].append({"layer": p["layer"], "net": "GND",
                              "exterior": [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords],
                              "holes": [[[round(x, 3), round(y, 3)] for x, y in h.coords] for h in g.interiors]})
    board["drc"] = drc(board, pours)
    board["stats"] = {"components": len(comps), "smd": sum(1 for c in comps if c["smd"] and c["kind"] != "MH"),
                      "tht": sum(1 for c in comps if not c["smd"] and c["kind"] != "MH"),
                      "pads": sum(len(c["pads"]) for c in comps), "traces": len(traces), "vias": len(vias),
                      "nets": len(nets), "trace_len_mm": round(sum(LineString(t["points"]).length for t in traces), 1)}
    board["_pour_geoms"] = pours
    return board


# ------------------------------------------------------------------ SVG views

NET_COL = {"GND": "#8b95a7", "V_SERVO": "#ff6b5b", "VIN_RAW": "#ff6b5b", "V_FUSED": "#ff8a7a", "V_IN": "#ff8a7a",
           "5V": "#ffa94d", "VSYS": "#ffa94d", "VBUS": "#ffa94d", "3V3": "#ffd43b", "BUS_DATA": "#5b9dff", "UART_TX": "#9775fa",
           "UART_RX": "#9775fa", "BUS_DIR": "#e599f7", "FSR_L": "#3ddc97", "FSR_R": "#3ddc97", "USB_DP": "#66d9e8",
           "USB_DM": "#66d9e8", "SDA": "#74c0fc", "SCL": "#74c0fc"}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pad_svg(p, fy, fill, hole="#0b0f14", inset=0.0):
    x, y, w, h = p["x"], fy(p["y"]), p["w"] - 2 * inset, p["h"] - 2 * inset
    out = ""
    if p["shape"] == "circle":
        out = f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{w / 2:.3f}" fill="{fill}"/>'
    elif p["shape"] == "oval":
        r = min(w, h) / 2
        out = f'<rect x="{x - w / 2:.3f}" y="{y - h / 2:.3f}" width="{w:.3f}" height="{h:.3f}" rx="{r:.3f}" fill="{fill}"/>'
    else:
        rr = min(w, h) * 0.15
        out = f'<rect x="{x - w / 2:.3f}" y="{y - h / 2:.3f}" width="{w:.3f}" height="{h:.3f}" rx="{rr:.3f}" fill="{fill}"/>'
    if p.get("drill"):
        out += f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{p["drill"] / 2:.3f}" fill="{hole}"/>'
    return out


def _ring_path(ring, fy):
    d = "M" + " L".join(f"{x:.3f},{fy(y):.3f}" for x, y in ring["exterior"]) + " Z"
    for h in ring.get("holes", []):
        d += " M" + " L".join(f"{x:.3f},{fy(y):.3f}" for x, y in h) + " Z"
    return d


def to_svg(board: dict, scale: float = 12.0, title: str = "") -> str:
    """EDA-style layer view (dark canvas, F.Cu red, B.Cu blue, pads gold, silkscreen) for docs/screenshots."""
    W, H = board["size_mm"]
    fy = lambda y: H - y  # noqa: E731
    pad_x, pad_top, pad_bot = 6.0, 9.0, 9.0
    vw, vh = W + 2 * pad_x, H + pad_top + pad_bot
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad_x} {-pad_top} {vw} {vh}" width="{vw * scale:.0f}" '
         f'height="{vh * scale:.0f}" font-family="JetBrains Mono, Menlo, Consolas, monospace">',
         f'<rect x="{-pad_x}" y="{-pad_top}" width="{vw}" height="{vh}" fill="#0d1117"/>',
         '<defs><pattern id="pg" width="2.54" height="2.54" patternUnits="userSpaceOnUse">'
         '<circle cx="0.1" cy="0.1" r="0.07" fill="#1c2430"/></pattern></defs>',
         f'<rect x="{-pad_x}" y="{-pad_top}" width="{vw}" height="{vh}" fill="url(#pg)"/>',
         f'<rect x="0" y="0" width="{W}" height="{H}" rx="1" fill="#0a2418" stroke="#d8c26a" stroke-width="0.2"/>']
    for ring in board.get("pour", []):
        col = "#2f6fd6" if ring.get("layer", "B.Cu") == "B.Cu" else "#c8443c"
        op = 0.22 if ring.get("layer", "B.Cu") == "B.Cu" else 0.16
        o.append(f'<path d="{_ring_path(ring, fy)}" fill="{col}" fill-opacity="{op}" fill-rule="evenodd"/>')
    for k in board.get("keepouts", []):
        o.append(f'<rect x="{k[0]:.2f}" y="{fy(k[3]):.2f}" width="{k[2] - k[0]:.2f}" height="{k[3] - k[1]:.2f}" fill="none" '
                 f'stroke="#e599f7" stroke-width="0.12" stroke-dasharray="0.6 0.4"/>')
    for layer, col, op in (("B.Cu", "#4f8fff", 0.85), ("F.Cu", "#e8615a", 0.95)):
        for t in board["traces"]:
            if t["layer"] != layer:
                continue
            pts = " ".join(f"{x:.3f},{fy(y):.3f}" for x, y in t["points"])
            o.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-opacity="{op}" stroke-width="{t["width"]}" '
                     f'stroke-linecap="round" stroke-linejoin="round"/>')
    for c in board["components"]:
        x0, y0, x1, y1 = c["outline"]
        if c.get("round"):
            o.append(f'<circle cx="{c["x"]:.2f}" cy="{fy(c["y"]):.2f}" r="{c["round"]:.2f}" fill="none" stroke="#e9e4c5" stroke-width="0.15"/>')
        elif c["kind"] != "MH":
            o.append(f'<rect x="{x0:.3f}" y="{fy(y1):.3f}" width="{x1 - x0:.3f}" height="{y1 - y0:.3f}" fill="none" '
                     f'stroke="#e9e4c5" stroke-width="0.15" stroke-opacity="0.9"/>')
        for p in c["pads"]:
            if p.get("np"):
                o.append(f'<circle cx="{p["x"]:.3f}" cy="{fy(p["y"]):.3f}" r="{p["drill"] / 2:.3f}" fill="#05070a" stroke="#6b7280" stroke-width="0.1"/>')
            else:
                o.append(_pad_svg(p, fy, "#d6b25e" if p.get("drill") else "#c9a24a"))
    for v in board.get("vias", []):
        o.append(f'<circle cx="{v["x"]:.3f}" cy="{fy(v["y"]):.3f}" r="{v["d"] / 2}" fill="#b8bec9"/>'
                 f'<circle cx="{v["x"]:.3f}" cy="{fy(v["y"]):.3f}" r="{v["drill"] / 2}" fill="#0b0f14"/>')
    for c in board["components"]:
        if c["kind"] == "MH":
            continue
        x0, y0, x1, y1 = c["outline"]
        o.append(f'<text x="{c["court"][0]:.2f}" y="{fy(c["court"][3]) - 0.6:.2f}" font-size="1.3" fill="#f2eda1">{_esc(c["ref"])}'
                 f'<tspan fill="#9aa3b2" font-size="1.1"> {_esc(c["value"])}</tspan></text>')
    drc = board.get("drc", {})
    ok = drc.get("pass")
    st = board.get("stats", {})
    o.append(f'<text x="0" y="{H + 4.5}" font-size="1.7" fill="#838c9d">{W:g} × {H:g} mm · 2 layers · 1.6 mm FR4 · '
             f'{st.get("components", 0)} parts · {st.get("traces", 0)} traces · {st.get("vias", 0)} vias</text>')
    o.append(f'<text x="{W}" y="{H + 4.5}" text-anchor="end" font-size="1.7" font-weight="700" fill="{"#3dd68c" if ok else "#ff6b6b"}">'
             f'{"DRC PASS" if ok else "DRC FAIL"}</text>')
    if title:
        o.append(f'<text x="0" y="-4" font-size="2.2" font-weight="700" fill="#e6edf3">{_esc(title)}</text>')
    o.append("</svg>")
    return "".join(o)


# ------------------------------------------------------------------ stroke font (silkscreen text in Gerbers)
# glyphs on a 4 x 6 grid (x right, y up); each glyph is a list of polylines
_F = {
    "0": [[(0, 0), (4, 0), (4, 6), (0, 6), (0, 0), (4, 6)]], "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]], "3": [[(0, 6), (4, 6), (2, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]], "5": [[(4, 6), (0, 6), (0, 3), (3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "6": [[(4, 6), (1, 6), (0, 5), (0, 0), (4, 0), (4, 3), (0, 3)]], "7": [[(0, 6), (4, 6), (1, 0)]],
    "8": [[(0, 0), (4, 0), (4, 6), (0, 6), (0, 0)], [(0, 3), (4, 3)]], "9": [[(0, 0), (3, 0), (4, 1), (4, 6), (0, 6), (0, 3), (4, 3)]],
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]], "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "C": [[(4, 6), (0, 6), (0, 0), (4, 0)]], "D": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]], "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [[(4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (2, 3)]], "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 0), (3, 0)], [(1, 6), (3, 6)], [(2, 0), (2, 6)]], "J": [[(4, 6), (4, 0), (0, 0), (0, 2)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 3), (4, 0)]], "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]], "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [[(0, 0), (4, 0), (4, 6), (0, 6), (0, 0)]], "P": [[(0, 0), (0, 6), (4, 6), (4, 3), (0, 3)]],
    "Q": [[(0, 0), (4, 0), (4, 6), (0, 6), (0, 0)], [(2, 2), (4, -1)]], "R": [[(0, 0), (0, 6), (4, 6), (4, 3), (0, 3), (4, 0)]],
    "S": [[(4, 6), (0, 6), (0, 3), (4, 3), (4, 0), (0, 0)]], "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 0), (4, 0), (4, 6)]], "V": [[(0, 6), (2, 0), (4, 6)]], "W": [[(0, 6), (1, 0), (2, 3), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]], "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]], "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "-": [[(1, 3), (3, 3)]], "+": [[(0, 3), (4, 3)], [(2, 1), (2, 5)]], ".": [[(2, 0), (2, 0.5)]], "/": [[(0, 0), (4, 6)]],
    "_": [[(0, 0), (4, 0)]], ":": [[(2, 1), (2, 1.5)], [(2, 4.5), (2, 5)]], " ": [],
}


def text_strokes(text: str, x: float, y: float, h: float = 1.0) -> list:
    """Polylines (board mm) for `text` with its baseline-left at (x, y), cap height h."""
    k = h / 6.0
    out = []
    cx = x
    for ch in text.upper():
        for line in _F.get(ch, _F["-"] if ch not in _F else []):
            out.append([(cx + px * k, y + py * k) for px, py in line])
        cx += 5.5 * k
    return out


# ------------------------------------------------------------------ Gerber / Excellon / JLC outputs

def _gfmt(v):
    return f"{int(round(v * 1e6))}"


class _Gerber:
    def __init__(self, function, polarity="Positive", comment=""):
        self.ap, self.body = {}, []
        self.head = ["%TF.GenerationSoftware,Morph,morph.robot.pcb,1.0*%", f"%TF.FileFunction,{function}*%",
                     f"%TF.FilePolarity,{polarity}*%", "%FSLAX46Y46*%", "%MOMM*%", f"G04 Morph {comment}*", "%LPD*%", "G01*"]
        self.cur = None

    def aperture(self, shape, w, h=None):
        h = w if h is None else h
        key = (shape, round(w, 4), round(h, 4))
        if key not in self.ap:
            code = 10 + len(self.ap)
            if shape == "C":
                self.ap[key] = (code, f"%ADD{code}C,{w:.4f}*%")
            elif shape == "O":
                self.ap[key] = (code, f"%ADD{code}O,{w:.4f}X{h:.4f}*%")
            else:
                self.ap[key] = (code, f"%ADD{code}R,{w:.4f}X{h:.4f}*%")
        return self.ap[key][0]

    def use(self, code):
        if self.cur != code:
            self.body.append(f"D{code}*")
            self.cur = code

    def flash(self, shape, w, h, x, y):
        self.use(self.aperture(shape, w, h))
        self.body.append(f"X{_gfmt(x)}Y{_gfmt(y)}D03*")

    def line(self, pts, width):
        self.use(self.aperture("C", width))
        for i, (x, y) in enumerate(pts):
            self.body.append(f"X{_gfmt(x)}Y{_gfmt(y)}D0{2 if i == 0 else 1}*")

    def region(self, ring):
        self.body.append("G36*")
        for i, (x, y) in enumerate(ring):
            self.body.append(f"X{_gfmt(x)}Y{_gfmt(y)}D0{2 if i == 0 else 1}*")
        self.body.append("G37*")

    def text(self):
        return "\n".join(self.head + [v[1] for v in sorted(self.ap.values())] + self.body + ["M02*"]) + "\n"


def _hole_free(poly):
    """Split a polygon with holes into hole-free pieces (Gerber regions cannot carry holes)."""
    if isinstance(poly, MultiPolygon):
        return [q for g in poly.geoms for q in _hole_free(g)]
    if not isinstance(poly, Polygon) or poly.is_empty:
        return []
    if not poly.interiors:
        return [poly]
    hx = poly.interiors[0].centroid.x
    y0, y1 = poly.bounds[1] - 1, poly.bounds[3] + 1
    pieces = split(poly, LineString([(hx, y0), (hx, y1)]))
    out = []
    for g in pieces.geoms:
        out += _hole_free(g)
    return out


def _pad_ap(p, grow=0.0):
    w, h = p["w"] + 2 * grow, p["h"] + 2 * grow
    if p["shape"] == "circle":
        return "C", w, w
    if p["shape"] == "oval":
        return "O", w, h
    return "R", w, h


def _outline_pts(W, H, r=1.0, n=6):
    pts = []
    for cx, cy, a0 in ((W - r, r, -90), (W - r, H - r, 0), (r, H - r, 90), (r, r, 180)):
        for k in range(n + 1):
            a = math.radians(a0 + 90 * k / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts + [pts[0]]


def silk_lines(board) -> list:
    """[(polyline, width)] for the top silkscreen: body outlines, pin-1 marks, reference designators, title."""
    out = []
    for c in board["components"]:
        if c["kind"] == "MH":
            continue
        x0, y0, x1, y1 = c["outline"]
        if c.get("round"):
            r = c["round"]
            out.append(([(c["x"] + r * math.cos(2 * math.pi * k / 40), c["y"] + r * math.sin(2 * math.pi * k / 40)) for k in range(41)], 0.15))
            out.append(([(c["x"] - r * 0.95, c["y"] + 0.6 * r), (c["x"] - r * 0.95 + 1.2, c["y"] + 0.6 * r)], 0.15))   # + mark
        else:
            out.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], 0.15))
        if len(c["pads"]) > 3 and c["kind"] in ("box", "conn"):
            p1 = next((p for p in c["pads"] if p["name"] in ("1", "A1B12")), None)
            if p1:
                dx = -0.9 if p1["x"] <= c["x"] else 0.9
                out.append(([(p1["x"] + dx, p1["y"]), (p1["x"] + dx, p1["y"] + 0.01)], 0.5))
        W_, H_ = board["size_mm"]
        tw = len(c["ref"]) * 5.5 / 6.0 - 0.1
        tx, ty = max(c["court"][0], 0.6), c["court"][3] + 0.45
        if ty + 1.0 > H_ - 0.3:          # top-edge part: put the designator beside the body
            tx, ty = c["outline"][0] - tw - 0.6, c["outline"][1] + 0.4
            if tx < 0.6:
                tx = c["outline"][2] + 0.6
        if c.get("overhang") and c["court"][0] < 0:     # left-edge connector hanging off the board
            tx, ty = 0.8, c["court"][3] + 0.45
        tx = min(tx, W_ - tw - 0.6)
        for line in text_strokes(c["ref"], tx, ty, 1.0):
            out.append((line, 0.15))
    W, H = board["size_mm"]
    title = board.get("silk_title", "")
    if title:
        for line in text_strokes(title, 6.0, 1.2, 1.2):
            out.append((line, 0.18))
    return out


def write_gerbers(board: dict, outdir: str, name: str = "morph_ctrl") -> list:
    os.makedirs(outdir, exist_ok=True)
    W, H = board["size_mm"]
    pours = board.get("_pour_geoms") or [{"layer": r["layer"], "geom": Polygon(r["exterior"], r["holes"])} for r in board["pour"]]
    layers = {}
    for li, (lname, fn) in enumerate((("F.Cu", "Copper,L1,Top"), ("B.Cu", "Copper,L2,Bot"))):
        g = _Gerber(fn, comment=lname)
        for p in pours:
            if p["layer"] == lname:
                for piece in _hole_free(p["geom"]):
                    g.region(list(piece.exterior.coords))
        for t in board["traces"]:
            if t["layer"] == lname:
                g.line(t["points"], t["width"])
        for c in board["components"]:
            for p in c["pads"]:
                if p.get("np") or lname not in p.get("layers", []):
                    continue
                g.flash(*_pad_ap(p), p["x"], p["y"])
        for v in board["vias"]:
            g.flash("C", v["d"], v["d"], v["x"], v["y"])
        layers[f"{name}-{lname.replace('.', '_')}.gbr"] = g
    for lname, fn in (("F.Mask", "Soldermask,Top"), ("B.Mask", "Soldermask,Bot")):
        g = _Gerber(fn, "Negative", lname)
        for c in board["components"]:
            for p in c["pads"]:
                if p.get("np"):
                    g.flash("C", p["drill"] + 0.1, p["drill"] + 0.1, p["x"], p["y"])
                    continue
                cu = "F.Cu" if lname == "F.Mask" else "B.Cu"
                if cu in p.get("layers", []):
                    g.flash(*_pad_ap(p, 0.05), p["x"], p["y"])
        layers[f"{name}-{lname.replace('.', '_')}.gbr"] = g
    g = _Gerber("Paste,Top", comment="F.Paste")
    for c in board["components"]:
        for p in c["pads"]:
            if not p.get("np") and not p.get("drill") and "F.Cu" in p.get("layers", []):
                s, w, h = _pad_ap(p, -0.02)
                g.flash(s, w, h, p["x"], p["y"])
    layers[f"{name}-F_Paste.gbr"] = g
    g = _Gerber("Legend,Top", comment="F.SilkS")
    for line, w in silk_lines(board):
        g.line(line, w)
    layers[f"{name}-F_Silkscreen.gbr"] = g
    g = _Gerber("Profile,NP", comment="Edge.Cuts")
    g.line(_outline_pts(W, H), 0.1)
    layers[f"{name}-Edge_Cuts.gbr"] = g
    files = {}
    for fn, g in layers.items():
        files[fn] = g.text()
    pth = [(p["x"], p["y"], p["drill"]) for c in board["components"] for p in c["pads"] if p.get("drill") and not p.get("np")]
    pth += [(v["x"], v["y"], v["drill"]) for v in board["vias"]]
    npth = [(p["x"], p["y"], p["drill"]) for c in board["components"] for p in c["pads"] if p.get("np")]
    for fn, holes, plated in ((f"{name}-PTH.drl", pth, True), (f"{name}-NPTH.drl", npth, False)):
        tools = sorted({round(d, 3) for _, _, d in holes})
        L = ["M48", f"; DRILL file Morph pcb.py, {'plated' if plated else 'non-plated'} holes",
             "; FORMAT={-:-/ absolute / metric / decimal}", f"; #@! TF.FileFunction,{'Plated' if plated else 'NonPlated'},1,2,{'PTH' if plated else 'NPTH'}",
             "FMAT,2", "METRIC"]
        L += [f"T{i + 1}C{d:.3f}" for i, d in enumerate(tools)] + ["%", "G90", "G05"]
        for i, d in enumerate(tools):
            L.append(f"T{i + 1}")
            L += [f"X{x:.3f}Y{y:.3f}" for x, y, dd in holes if round(dd, 3) == d]
        L.append("M30")
        files[fn] = "\n".join(L) + "\n"
    for fn, txt in files.items():
        with open(os.path.join(outdir, fn), "w") as fh:
            fh.write(txt)
    with zipfile.ZipFile(os.path.join(outdir, "gerbers.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for fn in files:
            z.write(os.path.join(outdir, fn), fn)
    return sorted(files) + ["gerbers.zip"]


def jlc_files(board: dict, circuit, outdir: str) -> dict:
    """JLCPCB assembly BOM (Comment, Designator, Footprint, LCSC Part #) and CPL (pick-and-place)."""
    groups = {}
    by_ref = {c.ref: c for c in circuit.comps}
    for comp in board["components"]:
        c = by_ref[comp["ref"]]
        if c.kind == "MH":
            continue
        key = (c.value, c.footprint, c.lcsc or "verify", c.mpn)
        groups.setdefault(key, []).append(c.ref)
    bom_rows = []
    for (value, fp, lcsc, mpn), refs in sorted(groups.items(), key=lambda kv: kv[1][0]):
        bom_rows.append({"Comment": f"{value}" + (f" ({mpn})" if mpn and mpn != value else ""), "Designator": ",".join(sorted(refs, key=_refkey)),
                         "Footprint": fp, "LCSC Part #": lcsc if lcsc else "verify", "Qty": len(refs)})
    with open(os.path.join(outdir, "jlc_bom.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Comment", "Designator", "Footprint", "LCSC Part #", "Qty"])
        w.writeheader()
        w.writerows(bom_rows)
    cpl = []
    for comp in board["components"]:
        if comp["kind"] == "MH":
            continue
        cpl.append({"Designator": comp["ref"], "Mid X": f"{comp['x']:.3f}mm", "Mid Y": f"{comp['y']:.3f}mm", "Layer": "Top",
                    "Rotation": comp["rot"]})
    with open(os.path.join(outdir, "jlc_cpl.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        w.writeheader()
        w.writerows(sorted(cpl, key=lambda r: _refkey(r["Designator"])))
    n_verify = sum(1 for r in bom_rows if r["LCSC Part #"] == "verify")
    return {"bom": "jlc_bom.csv", "cpl": "jlc_cpl.csv", "lines": len(bom_rows), "verify_lines": n_verify,
            "placements": len(cpl), "note": "CPL rotations follow the Morph footprint zero; confirm against JLCPCB's "
            "3D preview before ordering (their library zero differs for some packages)"}


def _refkey(ref):
    import re
    m = re.match(r"([A-Z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


# ------------------------------------------------------------------ cache + one-call build

def _cache_key(circuit, W, H, hc, pwr) -> str:
    import hashlib
    blob = json.dumps({"v": 7, "W": W, "H": H, "hc": list(hc), "pwr": list(pwr),
                       "c": [(c.ref, c.value, c.footprint, sorted(c.place.items(), key=str), [(p.num, p.net) for p in c.pins])
                             for c in circuit.comps if c.board]}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


def build(circuit, W, H, outdir, hc_nets=(), pwr_nets=(), name="morph_ctrl", title="", cache=True) -> dict:
    """Generate (or load from cache) the board, write board.json/.svg, Gerbers, drill, JLC BOM/CPL."""
    import tempfile
    cdir = os.environ.get("MORPH_PCB_CACHE", os.path.join(tempfile.gettempdir(), "morph_pcb_cache"))
    key = _cache_key(circuit, W, H, hc_nets, pwr_nets)
    path = os.path.join(cdir, f"{key}.json")
    board = None
    if cache and os.path.exists(path):
        try:
            board = json.load(open(path))
        except Exception:
            board = None
    if board is None:
        board = generate(circuit, W, H, hc_nets=hc_nets, pwr_nets=pwr_nets, name=name)
        board.pop("_pour_geoms", None)
        if cache:
            os.makedirs(cdir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(board, fh)
            os.replace(tmp, path)
    board["silk_title"] = title
    os.makedirs(outdir, exist_ok=True)
    gfiles = write_gerbers(board, os.path.join(outdir, "gerbers"), name)
    jlc = jlc_files(board, circuit, outdir)
    with open(os.path.join(outdir, "board.json"), "w") as fh:
        json.dump(board, fh)
    with open(os.path.join(outdir, "board.svg"), "w") as fh:
        fh.write(to_svg(board, title=title))
    with open(os.path.join(outdir, "board_render.svg"), "w") as fh:
        fh.write(render_svg(board, title=title))
    return {"board": board, "gerbers": [f"gerbers/{f}" for f in gfiles], "jlc": jlc}


def render_svg(board: dict, scale: float = 14.0, title: str = "") -> str:
    """Photo-style render of the assembled board: green mask, ENIG pads, white legend, component bodies."""
    W, H = board["size_mm"]
    fy = lambda y: H - y  # noqa: E731
    m = 5.0
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-m} {-m - 4} {W + 2 * m} {H + 2 * m + 4}" '
         f'width="{(W + 2 * m) * scale:.0f}" height="{(H + 2 * m + 4) * scale:.0f}" font-family="Inter, Helvetica, Arial, sans-serif">',
         '<defs>'
         '<linearGradient id="mask" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#13784a"/><stop offset="1" stop-color="#0b5535"/></linearGradient>'
         '<linearGradient id="gold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f1d58a"/><stop offset="1" stop-color="#c79a3c"/></linearGradient>'
         '<linearGradient id="can" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e3e7ec"/><stop offset=".55" stop-color="#b9c0c9"/><stop offset="1" stop-color="#9aa3ae"/></linearGradient>'
         '<linearGradient id="ic" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a2b30"/><stop offset="1" stop-color="#131317"/></linearGradient>'
         '<linearGradient id="plas" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbf6ea"/><stop offset="1" stop-color="#ddd2bb"/></linearGradient>'
         '<radialGradient id="alu" cx=".4" cy=".35" r=".8"><stop offset="0" stop-color="#eef1f4"/><stop offset="1" stop-color="#9ba3ad"/></radialGradient>'
         '<filter id="sh" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0.25" dy="0.35" stdDeviation="0.3" flood-color="#000" flood-opacity=".45"/></filter>'
         '</defs>',
         f'<rect x="{-m}" y="{-m - 4}" width="{W + 2 * m}" height="{H + 2 * m + 4}" fill="#0d1117"/>',
         f'<rect x="0.15" y="0.35" width="{W}" height="{H}" rx="1" fill="#000" opacity=".5"/>',
         f'<rect x="0" y="0" width="{W}" height="{H}" rx="1" fill="url(#mask)"/>']
    cu = "#1f8a55"
    for ring in board.get("pour", []):
        if ring.get("layer") == "F.Cu":
            o.append(f'<path d="{_ring_path(ring, fy)}" fill="{cu}" fill-rule="evenodd" opacity=".55"/>')
    for t in board["traces"]:
        if t["layer"] == "F.Cu":
            pts = " ".join(f"{x:.3f},{fy(y):.3f}" for x, y in t["points"])
            o.append(f'<polyline points="{pts}" fill="none" stroke="{cu}" stroke-width="{t["width"]}" stroke-linecap="round" stroke-linejoin="round"/>')
    for v in board.get("vias", []):
        o.append(f'<circle cx="{v["x"]:.3f}" cy="{fy(v["y"]):.3f}" r="{v["d"] / 2}" fill="{cu}"/>'
                 f'<circle cx="{v["x"]:.3f}" cy="{fy(v["y"]):.3f}" r="{v["drill"] / 2}" fill="#06140d"/>')
    for c in board["components"]:
        for p in c["pads"]:
            if p.get("np"):
                if c["kind"] == "MH":
                    o.append(f'<circle cx="{p["x"]:.3f}" cy="{fy(p["y"]):.3f}" r="{p["drill"] / 2 + 0.9}" fill="url(#gold)"/>')
                o.append(f'<circle cx="{p["x"]:.3f}" cy="{fy(p["y"]):.3f}" r="{p["drill"] / 2}" fill="#05070a"/>')
            elif "F.Cu" in p.get("layers", []):
                o.append(_pad_svg(p, fy, "url(#gold)", "#1a1206"))
    for line, w in silk_lines(board):
        pts = " ".join(f"{x:.3f},{fy(y):.3f}" for x, y in line)
        o.append(f'<polyline points="{pts}" fill="none" stroke="#f3f1e7" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"/>')
    for c in board["components"]:
        o.append(_body_svg(c, fy))
    if title:
        o.append(f'<text x="0" y="-1.6" font-size="2.0" font-weight="600" fill="#e6edf3">{_esc(title)}</text>')
    st = board.get("stats", {})
    o.append(f'<text x="{W}" y="-1.6" font-size="1.5" fill="#8b95a7" text-anchor="end">{W:g} × {H:g} mm · '
             f'{st.get("smd", 0)} SMD + {st.get("tht", 0)} THT · ENIG · green mask</text>')
    o.append("</svg>")
    return "".join(o)


def _body_svg(c, fy):
    k, fp, ref, val = c["kind"], c.get("footprint", ""), c["ref"], c["value"]
    x0, y0, x1, y1 = c["outline"]
    X, Y, Wd, Hd = x0, fy(y1), x1 - x0, y1 - y0
    cx, cy = c["x"], fy(c["y"])
    horiz = Wd >= Hd
    fs = max(0.5, min(1.1, min(Wd, Hd) * 0.32))
    lab = lambda txt, col="#c9cdd3", size=fs: (f'<text x="{cx:.2f}" y="{cy + size * 0.35:.2f}" font-size="{size:.2f}" '  # noqa: E731
                                               f'fill="{col}" text-anchor="middle" font-weight="600">{_esc(txt)}</text>')
    if k == "MH":
        return ""
    if fp in ("R0603", "C0603", "C0805", "LED0603", "L_4x4", "FUSE_2410"):
        body = {"R0603": "#1c1c1f", "C0603": "#b8925a", "C0805": "#b8925a", "LED0603": "#e8f5e9", "L_4x4": "#2b2d33",
                "FUSE_2410": "#f0eadc"}[fp]
        e = 0.28 * (Wd if horiz else Hd)
        s = [f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.12" fill="{body}"/>']
        if fp != "L_4x4":
            if horiz:
                s.append(f'<rect x="{X:.3f}" y="{Y:.3f}" width="{e:.3f}" height="{Hd:.3f}" fill="#cfd4da"/>'
                         f'<rect x="{X + Wd - e:.3f}" y="{Y:.3f}" width="{e:.3f}" height="{Hd:.3f}" fill="#cfd4da"/>')
            else:
                s.append(f'<rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{e:.3f}" fill="#cfd4da"/>'
                         f'<rect x="{X:.3f}" y="{Y + Hd - e:.3f}" width="{Wd:.3f}" height="{e:.3f}" fill="#cfd4da"/>')
        if fp == "L_4x4":
            s.append(f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{min(Wd, Hd) * 0.32:.3f}" fill="none" stroke="#6b6f78" stroke-width="0.18"/>')
        if fp == "LED0603":
            s.append(f'<rect x="{cx - 0.3:.3f}" y="{cy - 0.25:.3f}" width="0.6" height="0.5" fill="{"#46d36b" if "PWR" in val else "#ff4d4d"}" opacity=".85"/>')
        if fp == "R0603":
            s.append(f'<text x="{cx:.2f}" y="{cy + 0.18:.2f}" font-size="0.5" fill="#e8e8e8" text-anchor="middle">{_esc(val.replace("k", "K"))}</text>')
        s.append("</g>")
        return "".join(s)
    if fp in ("SOD123", "SMA", "SMB"):
        band = (f'<rect x="{X + Wd * 0.18:.3f}" y="{Y:.3f}" width="{Wd * 0.1:.3f}" height="{Hd:.3f}" fill="#9aa1ab"/>' if horiz else
                f'<rect x="{X:.3f}" y="{Y + Hd * 0.72:.3f}" width="{Wd:.3f}" height="{Hd * 0.1:.3f}" fill="#9aa1ab"/>')
        return f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.15" fill="url(#ic)"/>{band}</g>'
    if fp == "ESP32S3_WROOM1":
        # module PCB, shield can over the pad field, printed antenna at the top
        return (f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.3" fill="#1b2a3a"/>'
                f'<rect x="{X + 0.9:.3f}" y="{Y + 6.9:.3f}" width="{Wd - 1.8:.3f}" height="{Hd - 7.8:.3f}" rx="0.4" fill="url(#can)"/>'
                f'<path d="M{X + 1.5:.2f},{Y + 4.8:.2f} h3 v-2.6 h2 v2.6 h2 v-2.6 h2 v2.6 h2 v-2.6 h2 v2.6 h1.5" fill="none" stroke="#d9b65a" stroke-width="0.45"/>'
                f'<text x="{cx:.2f}" y="{Y + 13:.2f}" font-size="1.25" fill="#3b4350" text-anchor="middle" font-weight="700">ESPRESSIF</text>'
                f'<text x="{cx:.2f}" y="{Y + 15:.2f}" font-size="1.05" fill="#4b5563" text-anchor="middle">ESP32-S3-WROOM-1</text>'
                f'<text x="{cx:.2f}" y="{Y + 16.6:.2f}" font-size="0.8" fill="#6b7280" text-anchor="middle">N8R8 · FCC ID 2AC7Z</text></g>')
    if fp == "USBC_16P":
        return (f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y - 0.6:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.8" fill="url(#can)"/>'
                f'<rect x="{X + 1.2:.3f}" y="{Y + 0.2:.3f}" width="{Wd - 2.4:.3f}" height="1.2" rx="0.5" fill="#2a2f36"/></g>')
    if fp == "XT30PW":
        return (f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.6" fill="#e3b021"/>'
                f'<rect x="{X + 0.8:.3f}" y="{Y + 1.0:.3f}" width="{Wd * 0.55:.3f}" height="{Hd - 2:.3f}" rx="0.4" fill="#c9960f"/>'
                f'<text x="{cx - 1:.2f}" y="{cy + 0.4:.2f}" font-size="1.0" fill="#5a4300" text-anchor="middle" font-weight="700">XT30</text></g>')
    if fp == "CP_D10":
        r = c.get("round") or 5.0
        return (f'<g filter="url(#sh)"><circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" fill="url(#alu)"/>'
                f'<path d="M{cx - r:.3f},{cy:.3f} A{r},{r} 0 0 1 {cx - r * 0.5:.3f},{cy - r * 0.866:.3f} L{cx - r * 0.5:.3f},{cy + r * 0.866:.3f} A{r},{r} 0 0 1 {cx - r:.3f},{cy:.3f}" fill="#2a3340"/>'
                f'<path d="M{cx + 0.6:.2f},{cy - 2.2:.2f} v4.4 M{cx - 1.6:.2f},{cy:.2f} h4.4" stroke="#7c8591" stroke-width="0.25"/>'
                f'<text x="{cx + 0.8:.2f}" y="{cy + r * 0.62:.2f}" font-size="0.9" fill="#3b4350" text-anchor="middle">{_esc(val)}</text></g>')
    if fp == "SW_SMD_5x5":
        return (f'<g filter="url(#sh)"><rect x="{cx - 2.55:.3f}" y="{cy - 2.55:.3f}" width="5.1" height="5.1" rx="0.3" fill="url(#can)"/>'
                f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="1.5" fill="#1f2126"/></g>')
    if fp.startswith("HDR_3x") or fp in ("CONN_2P5_3", "JST_PH_2", "JST_XH_2", "JST_SH_4"):
        holes = "".join(f'<rect x="{p["x"] - 0.4:.3f}" y="{fy(p["y"]) - 0.4:.3f}" width="0.8" height="0.8" fill="#2a2420"/>'
                        for p in c["pads"] if p.get("drill"))
        col = "#1d1f24" if fp.startswith("HDR") else "url(#plas)"
        return (f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.25" fill="{col}"/>{holes}</g>')
    # generic IC (SOT, SSOP, TSSOP, SOT223, TO252)
    s = [f'<g filter="url(#sh)"><rect x="{X:.3f}" y="{Y:.3f}" width="{Wd:.3f}" height="{Hd:.3f}" rx="0.15" fill="url(#ic)"/>']
    s.append(f'<circle cx="{X + 0.45:.3f}" cy="{Y + 0.45:.3f}" r="0.2" fill="#3c3f46"/>')
    txt = val.split("-")[0][:10]
    size = min(0.85, fs, (max(Wd, Hd) * 0.85) / max(len(txt) * 0.62, 1))
    if min(Wd, Hd) > 1.8 and size >= 0.45:
        s.append(lab(txt, "#9aa1ab", size))
    s.append("</g>")
    return "".join(s)
