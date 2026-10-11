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

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import split, unary_union

GRID = 0.25
CLEAR = 0.2            # copper-to-copper, mm (JLCPCB 2-layer minimum is 0.127)
MARGIN = 0.05          # rasterisation safety so diagonal steps never undercut CLEAR
EDGE_CLEAR = 0.4       # copper to board edge
POUR_CLEAR = 0.3       # pour to other nets
VIA_D, VIA_DRILL = 0.6, 0.3
CLASSES = {"sig": 0.25, "pwr": 0.6, "hc": 2.0}
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
        return {"pads": pads, "body": (-4.47, 0.75, 4.47, 8.1), "court": (-5.1, -0.75, 5.1, 8.1), "front": (0, 1),
                "height": 3.2, "overhang": 0.6, "fcu_keepout": (-4.0, 0.9, 4.0, 8.1)}
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
                              "net": pin.net if pin else None})


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
        band = self._band(c, court)
        top = court[3] > H - 2.5
        for r, ref, kind in self.rects:
            if _overlap(court, r, 0.25):
                return False
            if not top and _overlap(band, r, 0.05) and kind != "band":
                return False
            if not top and kind == "band" and _overlap(band, r, 0.05):
                return False
            if kind == "band" and _overlap(court, r, 0.05):
                return False
        for k in self.keepouts:
            if _overlap(court, k, 0.3):
                return False
        if not top and band[2] > W + 1.5:
            return False
        return True

    def commit(self, c, fp, x, y, rot):
        p = Placed(c, fp, x, y, rot)
        self.placed[c.ref] = p
        self.rects.append((p.court, c.ref, "court"))
        if p.court[3] < self.H - 2.5:
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
            if best is not None and r > (best[0] - 0.0) * 0.5 + 1.5:
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
            self.commit(c, fp, best[1], best[2], best[3])
        anchored = [c for c in comps if c.place.get("at_xy")]
        for c in anchored:
            fp = footprint(c.footprint)
            fx, fy = c.place["at_xy"]
            best = self.spiral(c, fp, fx * W, fy * H, [c.place.get("rot", 0)])
            if best is None:
                raise RuntimeError(f"cannot place {c.ref}")
            self.commit(c, fp, best[1], best[2], best[3])
        rest = [c for c in comps if c.ref not in self.placed]
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
    def __init__(self, W, H, nets: list):
        self.W, self.H = W, H
        self.nx, self.ny = int(round(W / GRID)) + 1, int(round(H / GRID)) + 1
        self.net_id = {n: i for i, n in enumerate(nets)}
        self.own = {(l, k): np.full((self.nx, self.ny), -1, dtype=np.int32) for l in (0, 1) for k in CLASSES}
        self.tree: dict = {}            # net id -> set of (layer, idx) copper centreline cells
        self.traces, self.vias = [], []
        xs = np.arange(self.nx) * GRID
        ys = np.arange(self.ny) * GRID
        self.X, self.Y = np.meshgrid(xs, ys, indexing="ij")
        # board edge keep-out (all classes, both layers)
        for (l, k), arr in self.own.items():
            r = EDGE_CLEAR + CLASSES[k] / 2 + MARGIN
            m = (self.X < r) | (self.Y < r) | (self.X > W - r) | (self.Y > H - r)
            arr[m] = -2

    def nid(self, net):
        if net not in self.net_id:
            self.net_id[net] = len(self.net_id)
        return self.net_id[net]

    def idx(self, i, j):
        return i * self.ny + j

    def ij(self, idx):
        return divmod(idx, self.ny)

    def mark(self, layers, shape, net):
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
              max_cost=None, bias_b=1.25):
        k = self.nid(net)
        n = self.nx * self.ny
        free = [None, None]
        for l in (0, 1):
            a = self.own[(l, cls)]
            free[l] = ((a == -1) | (a == k)).ravel().tobytes()
        via_ok = None
        if allow_via:
            va, vb = self.own[(0, "pwr")], self.own[(1, "pwr")]
            via_ok = (((va == -1) | (va == k)) & ((vb == -1) | (vb == k))).ravel().tobytes()
        ny = self.ny
        if window:
            wi0, wj0, wi1, wj1 = window
        else:
            wi0, wj0, wi1, wj1 = 1, 1, self.nx - 2, ny - 2
        gx, gy = goal
        tset = set(targets) if targets else set()
        steps = [(di * ny + dj, di, dj, 1.414 if di and dj else 1.0) for di, dj in DIRS]
        best = {}
        prev = {}
        pq = []
        for (l, c) in sources:
            if l not in layers:
                continue
            key = (c * 2 + l) * 9 + 8
            best[key] = 0.0
            i, j = divmod(c, ny)
            h = 0.0 if goal_fn else max(abs(i - gx), abs(j - gy)) + 0.414 * min(abs(i - gx), abs(j - gy))
            heapq.heappush(pq, (h * 1.15, 0.0, c, l, 8))
        exp = 0
        while pq:
            f, g, c, l, d = heapq.heappop(pq)
            key = (c * 2 + l) * 9 + d
            if g > best.get(key, 1e18):
                continue
            exp += 1
            if exp > 600000:
                return None
            if (goal_fn and goal_fn(c, l)) or (l, c) in tset:
                path = [(l, c)]
                while key in prev:
                    key = prev[key]
                    ll = (key // 9) % 2
                    path.append((ll, key // 18))
                return path[::-1]
            if max_cost and g > max_cost:
                continue
            i, j = divmod(c, ny)
            mult = bias_b if l == 1 else 1.0
            fl = free[l]
            for nd, (off, di, dj, base) in enumerate(steps):
                ni, nj = i + di, j + dj
                if ni < wi0 or nj < wj0 or ni > wi1 or nj > wj1:
                    continue
                nc = c + off
                if (l, nc) not in tset and not fl[nc]:
                    continue
                if di and dj and (not fl[c + di * ny] or not fl[c + dj]):
                    continue
                if d != 8:
                    t = abs(nd - d)
                    t = min(t, 8 - t)
                    if t == 4:
                        continue
                    tc = TURN_COST[t]
                else:
                    tc = 0.0
                ng = g + base * mult + tc
                nkey = (nc * 2 + l) * 9 + nd
                if ng < best.get(nkey, 1e18):
                    best[nkey] = ng
                    prev[nkey] = key
                    h = 0.0 if goal_fn else max(abs(ni - gx), abs(nj - gy)) + 0.414 * min(abs(ni - gx), abs(nj - gy))
                    heapq.heappush(pq, (ng + h * 1.15, ng, nc, l, nd))
            if allow_via and via_ok[c] and len(layers) == 2:
                ol = 1 - l
                if free[ol][c]:
                    ng = g + VIA_COST
                    nkey = (c * 2 + ol) * 9 + 8
                    if ng < best.get(nkey, 1e18):
                        best[nkey] = ng
                        prev[nkey] = key
                        h = 0.0 if goal_fn else max(abs(i - gx), abs(j - gy)) + 0.414 * min(abs(i - gx), abs(j - gy))
                        heapq.heappush(pq, (ng + h * 1.15, ng, c, ol, 8))
        return None

    def commit_path(self, net, path, width, start_xy=None, end_xy=None):
        """Turn a cell path into traces (+ vias at layer changes) and mark the rasters."""
        k = self.nid(net)
        runs, cur = [], [path[0]]
        for a, b in zip(path, path[1:]):
            if b[0] != a[0]:
                runs.append(cur)
                cur = [b]
                v = self.ij(b[1])
                self.add_via(net, v[0] * GRID, v[1] * GRID)
            else:
                cur.append(b)
        runs.append(cur)
        made = []
        for ri, run in enumerate(runs):
            pts = [self.ij(c) for _, c in run]
            pts = _simplify_ij(pts)
            xy = [[round(i * GRID, 4), round(j * GRID, 4)] for i, j in pts]
            if ri == 0 and start_xy is not None:
                xy = [list(start_xy)] + xy
            if ri == len(runs) - 1 and end_xy is not None:
                xy = xy + [list(end_xy)]
            xy = _dedupe(xy)
            layer = LAYERS[run[0][0]]
            if len(xy) >= 2:
                t = {"net": net, "layer": layer, "width": width, "points": xy}
                self.traces.append(t)
                made.append(t)
                for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
                    self.mark([run[0][0]], ("seg", x0, y0, x1, y1, width / 2), net)
            for l, c in run:
                self.tree.setdefault(k, set()).add((l, c))
        return made

    def add_via(self, net, x, y):
        self.vias.append({"x": round(x, 4), "y": round(y, 4), "d": VIA_D, "drill": VIA_DRILL, "net": net})
        self.mark([0, 1], ("circle", x, y, VIA_D / 2), net)
        k = self.nid(net)
        c = self.idx(int(round(x / GRID)), int(round(y / GRID)))
        self.tree.setdefault(k, set()).update({(0, c), (1, c)})


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


def generate(circuit, W: float, H: float, hc_nets=(), pwr_nets=(), mount_holes=None, name="board", seed_order=None) -> dict:
    """Place and route `circuit` (board parts only) on a W x H mm 2-layer board."""
    holes = mount_holes or [(3.5, 3.5), (W - 3.5, 3.5), (3.5, H - 3.5), (W - 3.5, H - 3.5)]
    placer = Placer(circuit, W, H, holes)
    placed = placer.run()
    nets = circuit.nets(board_only=True)
    net_names = sorted(nets)
    r = Router(W, H, net_names)
    pads = []
    for ref, pl in placed.items():
        for p in pl.pads:
            p["ref"] = ref
            pads.append(p)
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
    for pl in placed.values():   # antenna keep-out (both layers) + no F.Cu routing under module bodies
        fp = pl.fp
        if "keepout" in fp:
            k = _rect_rot(fp["keepout"], pl.rot, pl.x, pl.y)
            r.mark([0, 1], ("area", k[0], k[1], k[2], k[3]), None)
        if "fcu_keepout" in fp:
            k = _rect_rot(fp["fcu_keepout"], pl.rot, pl.x, pl.y)
            r.mark([0], ("area", k[0], k[1], k[2], k[3]), None)

    # terminals: pads grouped by (ref, num) = internally connected
    terms: dict = {}
    for p in pads:
        if p["net"] and not p["np"]:
            terms.setdefault(p["net"], {}).setdefault((p["ref"], p["num"]), []).append(p)

    def term_cells(group):
        out = []
        for p in group:
            for c in r.pad_cells(p):
                out.append((0, c))
                if p["drill"]:
                    out.append((1, c))
        return out

    def term_min(group):
        return max(min(p["w"], p["h"]) for p in group)

    def centre(group):
        return (float(np.mean([p["x"] for p in group])), float(np.mean([p["y"] for p in group])))

    unrouted = []
    order = [n for n in net_names if n != "GND"]
    span = {}
    for n in order:
        cs = [centre(g) for g in terms.get(n, {}).values()]
        span[n] = (max(x for x, _ in cs) - min(x for x, _ in cs) + max(y for _, y in cs) - min(y for _, y in cs)) if cs else 0
    rank = {"hc": 0, "pwr": 1, "sig": 2}
    order.sort(key=lambda n: (rank[net_class(n, hc_nets, pwr_nets)], span[n]))
    if seed_order:
        order = [n for n in seed_order if n in order] + [n for n in order if n not in seed_order]

    for net in order:
        groups = list(terms.get(net, {}).values())
        if len(groups) < 2:
            continue
        cls0 = net_class(net, hc_nets, pwr_nets)
        cen = np.mean([centre(g) for g in groups], axis=0)
        groups.sort(key=lambda g: math.hypot(centre(g)[0] - cen[0], centre(g)[1] - cen[1]))
        first = groups[0]
        k = r.nid(net)
        for c in term_cells(first):
            r.tree.setdefault(k, set()).add(c)
        done = [first]
        todo = groups[1:]
        while todo:
            # nearest remaining terminal to the connected tree
            tc = [(r.ij(c)) for _, c in r.tree.get(k, set())]
            arr = np.array(tc) * GRID
            todo.sort(key=lambda g: float(np.min(np.abs(arr[:, 0] - centre(g)[0]) + np.abs(arr[:, 1] - centre(g)[1]))))
            g = todo.pop(0)
            m = term_min(g)
            cls = cls0
            if cls == "hc" and (m < 1.0 or (len(done) == 1 and term_min(done[0]) < 1.0)):
                cls = "pwr"
            if cls == "pwr" and (m < 0.6 or (len(done) == 1 and term_min(done[0]) < 0.6)):
                cls = "sig"
            width = CLASSES[cls]
            tgt = term_cells(g)
            gx, gy = centre(g)
            goal = (gx / GRID, gy / GRID)
            srcs = list(r.tree.get(k, set()))
            allp = [r.ij(c) for _, c in srcs] + [r.ij(c) for _, c in tgt]
            pad_i = 32
            win = (max(min(a for a, _ in allp) - pad_i, 1), max(min(b for _, b in allp) - pad_i, 1),
                   min(max(a for a, _ in allp) + pad_i, r.nx - 2), min(max(b for _, b in allp) + pad_i, r.ny - 2))
            layers = (0, 1)
            path = r.astar(net, cls, srcs, tgt, goal, layers=layers, window=win)
            if path is None:
                path = r.astar(net, cls, srcs, tgt, goal, layers=layers)
            if path is None and cls != "sig":
                cls, width = "sig", CLASSES["sig"]
                path = r.astar(net, cls, srcs, tgt, goal, layers=layers)
            if path is None:
                unrouted.append({"net": net, "pins": [f"{p['ref']}.{p['num']}" for p in g]})
                continue
            # snap ends to pad centres when they land in a pad
            sxy = exy = None
            end_l, end_c = path[-1]
            for p in g:
                if end_c in r.pad_cells(p):
                    exy = (round(p["x"], 4), round(p["y"], 4))
                    break
            st_l, st_c = path[0]
            for gg in done:
                for p in gg:
                    if st_c in r.pad_cells(p) and (st_l == 0 or p["drill"]):
                        sxy = (round(p["x"], 4), round(p["y"], 4))
                        break
            r.commit_path(net, path, width, sxy, exy)
            for c in term_cells(g):
                r.tree[k].add(c)
            done.append(g)

    # GND: TH pads join both layers; every SMD GND pad gets a short stub to a via into the B.Cu plane
    gnd_pads = [p for p in pads if p["net"] == "GND" and not p["np"]]
    k_g = r.nid("GND")
    for p in gnd_pads:
        if p["drill"]:
            continue
        if p["w"] >= 3.0 and p["h"] >= 3.0:          # exposed pad: via-in-pad array
            for dx in (-0.9, 0.9):
                for dy in (-0.9, 0.9):
                    r.add_via("GND", round((p["x"] + dx) / GRID) * GRID, round((p["y"] + dy) / GRID) * GRID)
            continue
        reach = max(p["w"], p["h"]) / 2 + VIA_D / 2 + 0.25
        via_ok = None
        va, vb = r.own[(0, "pwr")], r.own[(1, "pwr")]
        via_ok = ((va == -1) | (va == k_g)) & ((vb == -1) | (vb == k_g))
        flat = via_ok.ravel()
        px, py = p["x"], p["y"]

        def goal_fn(c, l, px=px, py=py, reach=reach):
            if l != 0 or not flat[c]:
                return False
            i, j = divmod(c, r.ny)
            return math.hypot(i * GRID - px, j * GRID - py) >= reach

        src = [(0, c) for c in r.pad_cells(p)]
        path = r.astar("GND", "sig", src, None, (0, 0), layers=(0,), allow_via=False, goal_fn=goal_fn, max_cost=40)
        if path is None:
            unrouted.append({"net": "GND", "pins": [f"{p['ref']}.{p['num']}"], "note": "no room for a GND via"})
            continue
        r.commit_path("GND", path, 0.4 if min(p["w"], p["h"]) >= 0.6 else CLASSES["sig"], (round(px, 4), round(py, 4)), None)
        i, j = r.ij(path[-1][1])
        r.add_via("GND", i * GRID, j * GRID)
    # stitching vias on a 6 mm lattice where both planes have room
    hc0, hc1 = r.own[(0, "hc")], r.own[(1, "hc")]
    for x in np.arange(6.0, W - 3.0, 6.0):
        for y in np.arange(6.0, H - 3.0, 6.0):
            i, j = int(round(x / GRID)), int(round(y / GRID))
            ok = all(a[i0, j0] in (-1, k_g) for a in (hc0, hc1)
                     for i0 in (i - 2, i, i + 2) for j0 in (j - 2, j, j + 2) if 0 <= i0 < r.nx and 0 <= j0 < r.ny)
            if ok:
                r.add_via("GND", i * GRID, j * GRID)

    board = _assemble(circuit, placed, pads, r, W, H, unrouted, name)
    return board


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
    for v in board["vias"]:
        g = Point(v["x"], v["y"]).buffer(v["d"] / 2, quad_segs=12)
        for l in LAYERS:
            items.append((v["net"], l, g, "via", None))
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
        area = area.buffer(-0.12, join_style=2).buffer(0.12, join_style=2)   # drop slivers narrower than ~0.25 mm
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
        if it[3] == "pad":
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
    board = {"name": name, "size_mm": [round(W, 2), round(H, 2)], "layers": 2, "thickness_mm": 1.6, "copper_oz": 1,
             "components": comps, "nets": nets, "traces": r.traces, "vias": r.vias, "ratsnest": rats,
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
                      "pads": sum(len(c["pads"]) for c in comps), "traces": len(r.traces), "vias": len(r.vias),
                      "nets": len(nets), "trace_len_mm": round(sum(LineString(t["points"]).length for t in r.traces), 1)}
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
