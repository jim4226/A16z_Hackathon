"""Fabrication planning: process choice, DFM checks, machine files, time and cost.

Outputs per process:
  fdm_print  -> finger.stl, plate.stl (+ G-code if a slicer CLI is installed)
  laser_cut  -> cut.svg (kerf-compensated, both fingers nested)
  cnc_router -> job.nc (GRBL G-code: holes, multi-pass contour, holding tabs) + toolpath.svg
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

from .design import FingerGeometry, export_stl, to_mesh
from .materials import (CNC_FEEDS, LASER_SPEED, MACHINE_RATE_PER_HR, MATERIALS,
                        PRINT_FILL_FRACTION, PRINT_FLOW_MM3_S)
from .spec import Design

SHEET_THICKNESSES = (3.0, 4.5, 6.0, 9.5, 12.7)
MARGIN = 6.0
GAP = 8.0
RAPID_MM_MIN = 2000.0
SAFE_Z = 5.0


def choose_process(design: Design) -> str:
    mat = MATERIALS[design.finger.material]
    if design.fab.process != "auto":
        if design.fab.process not in mat.processes:
            raise ValueError(f"{mat.name} cannot be made by {design.fab.process}; options: {mat.processes}")
        return design.fab.process
    return mat.processes[0]


def nest(geom: FingerGeometry, qty: int):
    """Stack qty copies of every part in a column; returns translated polygons and stock size (x, y)."""
    polys = [p for _ in range(qty) for p in geom.part_polys().values()]
    parts, y, w_max = [], MARGIN, 0.0
    for p in polys:
        minx, miny, maxx, maxy = p.bounds
        parts.append(affinity.translate(p, MARGIN - minx, y - miny))
        y += (maxy - miny) + GAP
        w_max = max(w_max, maxx - minx)
    return parts, (w_max + 2 * MARGIN, y - GAP + MARGIN)


def dfm(design: Design, geom: FingerGeometry, process: str) -> list:
    f = design.finger
    warn = []
    if geom.sensor != "none" and f.width_mm < 19.5:
        warn.append(f"width {f.width_mm} mm cannot seat the {geom.sensor} (needs >= 19.5 mm)")
    if process in ("laser_cut", "cnc_router") and not any(abs(f.width_mm - s) < 0.05 for s in SHEET_THICKNESSES):
        warn.append(f"width {f.width_mm} mm is not a stock sheet thickness {SHEET_THICKNESSES}")
    if process == "cnc_router":
        r = design.fab.tool_d_mm / 2.0
        closed = geom.profile.buffer(r, quad_segs=16).buffer(-r, quad_segs=16)
        leftover = closed.area - geom.profile.area
        if leftover > 0.5:
            warn.append(f"inside corners sharper than tool radius {r:.2f} mm leave {leftover:.1f} mm^2 uncut (auto-filleted)")
        if f.mount_hole_d_mm < design.fab.tool_d_mm:
            warn.append(f"mount hole {f.mount_hole_d_mm} mm smaller than tool; drill separately")
        if process == "cnc_router" and f.material == "Al6061" and f.width_mm > 6.5:
            warn.append("aluminium deeper than 2x tool diameter: slow, chatter-prone on a hobby router")
    if process == "laser_cut" and f.material not in LASER_SPEED:
        warn.append(f"{f.material} is unsafe or unsupported on a CO2 laser")
    if min(f.thickness_mm, f.pad_depth_mm if f.pad != "flat" else 99) < 1.2:
        warn.append("feature thinner than 1.2 mm")
    return warn


# ---------------------------------------------------------------- printing

def find_slicer():
    for exe in ("prusa-slicer", "PrusaSlicer", "orca-slicer", "bambu-studio"):
        path = shutil.which(exe)
        if path:
            return path
    return None


def plan_print(design: Design, geom: FingerGeometry, outdir: str) -> dict:
    import trimesh

    mat = MATERIALS[design.finger.material]
    qty = design.fab.quantity
    stl = os.path.join(outdir, "finger.stl")
    info = export_stl(geom, stl)
    plate, y = [], 0.0
    for _ in range(qty):
        for poly in geom.part_polys().values():
            m = to_mesh(poly, geom.width_mm)
            m.apply_translation([0, y - m.bounds[0][1], 0])
            y += m.extents[1] + GAP
            plate.append(m)
    trimesh.util.concatenate(plate).export(os.path.join(outdir, "plate.stl"))

    vol = geom.volume_mm3 * qty
    minutes = vol * PRINT_FILL_FRACTION / PRINT_FLOW_MM3_S / 60.0 * 1.3 + 4.0
    grams = vol * PRINT_FILL_FRACTION / 1000.0 * mat.density_g_cc
    files = ["finger.stl", "plate.stl"]
    for name in (geom.parts or {}):
        files.append(f"finger_{name}.stl")
    sliced = None
    slicer = find_slicer()
    if slicer and "prusa" in os.path.basename(slicer).lower():
        gcode = os.path.join(outdir, "plate.gcode")
        try:
            subprocess.run([slicer, "--export-gcode", "--output", gcode, os.path.join(outdir, "plate.stl")],
                           check=True, capture_output=True, timeout=300)
            files.append("plate.gcode")
            sliced = gcode
        except Exception as exc:  # slicer present but failed; keep the STL path working
            sliced = f"slicer failed: {exc}"
    return {"files": files, "mesh": info, "minutes": round(minutes, 1), "material_g": round(grams, 1),
            "material_cost": round(grams / 1000.0 * mat.cost_per_kg, 2), "sliced": sliced,
            "machine_note": "print flat (profile on the bed): no supports, layers across the bending plane"}


# ---------------------------------------------------------------- laser

def plan_laser(design: Design, geom: FingerGeometry, outdir: str) -> dict:
    mat = MATERIALS[design.finger.material]
    kerf = 0.15
    parts, (sx, sy) = nest(geom, design.fab.quantity)
    paths, length = [], 0.0
    for p in parts:
        cut = p.buffer(kerf / 2, quad_segs=24)  # outside grows, holes shrink: kerf compensated
        for ring in [cut.exterior, *cut.interiors]:
            pts = np.asarray(ring.coords)
            length += ring.length
            paths.append("M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in pts) + " Z")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{sx:.2f}mm" height="{sy:.2f}mm" '
           f'viewBox="0 0 {sx:.2f} {sy:.2f}">\n'
           + "\n".join(f'<path d="{d}" fill="none" stroke="#ff0000" stroke-width="0.01"/>' for d in paths)
           + "\n</svg>\n")
    with open(os.path.join(outdir, "cut.svg"), "w") as fh:
        fh.write(svg)
    speed = LASER_SPEED.get(mat.name, 5.0) * (6.0 / max(design.finger.width_mm, 1.0))
    pierces = len(paths)
    minutes = (length / speed + pierces * 1.0) / 60.0 + 2.0
    stock_g = sx * sy * design.finger.width_mm / 1000.0 * mat.density_g_cc
    return {"files": ["cut.svg"], "cut_length_mm": round(length, 1), "minutes": round(minutes, 1),
            "stock_mm": [round(sx, 1), round(sy, 1), design.finger.width_mm],
            "material_cost": round(stock_g / 1000.0 * mat.cost_per_kg * 1.5, 2)}


# ---------------------------------------------------------------- CNC

class GcodeWriter:
    def __init__(self, feed, plunge):
        self.lines, self.feed, self.plunge = [], feed, plunge
        self.pos = np.array([0.0, 0.0, SAFE_Z])
        self.cut_mm = self.plunge_mm = self.rapid_mm = 0.0

    def emit(self, s):
        self.lines.append(s)

    def rapid(self, x=None, y=None, z=None):
        tgt = self.pos.copy()
        for i, v in enumerate((x, y, z)):
            if v is not None:
                tgt[i] = v
        self.rapid_mm += float(np.linalg.norm(tgt - self.pos))
        self.emit("G0 " + " ".join(f"{a}{v:.3f}" for a, v in zip("XYZ", (x, y, z)) if v is not None))
        self.pos = tgt

    def cut(self, x, y, z):
        tgt = np.array([x, y, z])
        d = tgt - self.pos
        if abs(d[2]) > 1e-9 and np.hypot(d[0], d[1]) < 1e-9:
            self.plunge_mm += abs(d[2])
            self.emit(f"G1 Z{z:.3f} F{self.plunge:.0f}")
        else:
            self.cut_mm += float(np.linalg.norm(d))
            self.emit(f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{self.feed:.0f}")
        self.pos = tgt

    def minutes(self):
        return self.cut_mm / self.feed + self.plunge_mm / self.plunge + self.rapid_mm / RAPID_MM_MIN


def _resample(ring, step=0.5):
    line = LineString(ring.coords)
    n = max(int(line.length / step), 8)
    return [line.interpolate(i / n, normalized=True) for i in range(n + 1)], line.length


def plan_cnc(design: Design, geom: FingerGeometry, outdir: str) -> dict:
    f = design.finger
    mat = MATERIALS[f.material]
    feeds = CNC_FEEDS[mat.name]
    tool_d = design.fab.tool_d_mm
    r = tool_d / 2.0
    depth = f.width_mm + 0.2          # cut through into spoilboard
    tab_h, tab_w = min(1.0, f.width_mm / 3), 4.0
    feed = min(feeds["rpm"] * feeds["flutes"] * feeds["chipload"], 2000.0)
    g = GcodeWriter(feed, feeds["plunge"])
    parts, (sx, sy) = nest(geom, design.fab.quantity)

    g.emit(f"( Morph CNC job: {design.name} / {f.material} {f.width_mm} mm plate )")
    g.emit(f"( stock {sx:.1f} x {sy:.1f} mm, origin lower-left, Z0 = stock top )")
    g.emit(f"( tool: {tool_d} mm flat end mill, {feeds['flutes']} flute )")
    g.emit("G21 G90 G17 G94")
    g.emit(f"M3 S{feeds['rpm']}")
    g.rapid(z=SAFE_Z)
    n_layers = max(math.ceil(depth / feeds["doc"]), 1)
    layers = [-depth * (i + 1) / n_layers for i in range(n_layers)]
    previews = []

    for part in parts:
        # 1) mount holes: peck-plunge (hole ~ tool size) or circular interpolation
        for ring in part.interiors:
            c = Polygon(ring).centroid
            hole_r = math.sqrt(Polygon(ring).area / math.pi)
            g.emit(f"( hole d={2*hole_r:.2f} )")
            if hole_r - r < 0.3:
                g.rapid(c.x, c.y)
                g.rapid(z=1.0)
                for z in layers:
                    g.cut(c.x, c.y, z)
                    g.rapid(z=1.0)
            else:
                path_r = hole_r - r
                g.rapid(c.x + path_r, c.y)
                g.rapid(z=1.0)
                for z in layers:
                    g.cut(c.x + path_r, c.y, z)
                    for k in range(1, 37):
                        a = 2 * math.pi * k / 36
                        g.cut(c.x + path_r * math.cos(a), c.y + path_r * math.sin(a), z)
            g.rapid(z=SAFE_Z)
            previews.append(Polygon(ring).buffer(-min(r, hole_r * 0.5)).exterior)
        # 2) outside contour, climb (clockwise), holding tabs on the final layers
        path = orient(Polygon(part.exterior).buffer(r, quad_segs=24), -1.0).exterior
        pts, perim = _resample(path)
        tabs = [0.25 * perim, 0.75 * perim]
        g.emit("( outside contour )")
        g.rapid(pts[0].x, pts[0].y)
        g.rapid(z=1.0)
        for z in layers:
            for i, p in enumerate(pts):
                s = perim * i / (len(pts) - 1)
                zz = z
                if z < -(depth - tab_h) and any(abs(s - t) < tab_w / 2 + r for t in tabs):
                    zz = -(depth - tab_h)
                g.cut(p.x, p.y, zz)
        g.rapid(z=SAFE_Z)
        previews.append(path)

    g.emit("M5")
    g.rapid(0, 0, 10)
    g.emit("M30")
    with open(os.path.join(outdir, "job.nc"), "w") as fh:
        fh.write("\n".join(g.lines) + "\n")

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 {sx+4:.1f} {sy+4:.1f}" '
           f'width="{(sx+4)*4:.0f}" height="{(sy+4)*4:.0f}">',
           f'<rect x="0" y="0" width="{sx:.2f}" height="{sy:.2f}" fill="#e9e4d8" stroke="#999" stroke-width="0.3"/>']
    for p in parts:
        svg.append(f'<path d="{_poly_d(p)}" fill="#9fb7d6" fill-rule="evenodd" stroke="none"/>')
    for ring in previews:
        d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in ring.coords)
        svg.append(f'<path d="{d}" fill="none" stroke="#d1495b" stroke-width="0.35"/>')
    svg.append("</svg>")
    with open(os.path.join(outdir, "toolpath.svg"), "w") as fh:
        fh.write("\n".join(svg))

    minutes = g.minutes() + 5.0  # + tool change / zeroing
    stock_g = sx * sy * f.width_mm / 1000.0 * mat.density_g_cc
    return {"files": ["job.nc", "toolpath.svg"], "gcode_lines": len(g.lines), "passes": n_layers,
            "feed_mm_min": round(feed), "rpm": feeds["rpm"], "doc_mm": feeds["doc"],
            "minutes": round(minutes, 1), "stock_mm": [round(sx, 1), round(sy, 1), f.width_mm],
            "material_cost": round(stock_g / 1000.0 * mat.cost_per_kg * 1.5, 2)}


def _poly_d(p):
    rings = [p.exterior, *p.interiors]
    return " ".join("M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in r.coords) + " Z" for r in rings)


def plan(design: Design, geom: FingerGeometry, outdir: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    process = choose_process(design)
    planner = {"fdm_print": plan_print, "laser_cut": plan_laser, "cnc_router": plan_cnc}[process]
    out = planner(design, geom, outdir)
    out["process"] = process
    out["dfm_warnings"] = dfm(design, geom, process)
    out["machine_cost"] = round(out["minutes"] / 60.0 * MACHINE_RATE_PER_HR[process], 2)
    out["total_cost"] = round(out["machine_cost"] + out["material_cost"], 2)
    return out


def bom(design: Design, fab_plan: dict) -> list:
    q = design.fab.quantity
    return [
        {"item": f"finger ({design.finger.material}, {fab_plan['process']})", "qty": q,
         "cost": fab_plan["total_cost"]},
        {"item": "M3 x 10 socket head cap screw", "qty": 2 * q, "cost": round(0.12 * 2 * q, 2)},
        {"item": "friction tape pad 20x30 mm (optional)", "qty": q, "cost": round(0.30 * q, 2)},
        {"item": design.gripper.name, "qty": 1, "cost": 0.0, "note": "existing hardware"},
    ]
