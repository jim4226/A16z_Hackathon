"""Carrier board for the force-limited gripper: netlist -> placement -> routing -> pour -> Gerbers.

Through-hole parts on a 0.1" grid, so the same layout can be hand-soldered on
perfboard today, sent to a PCB fab as Gerbers, or opened in KiCad (morph/kicad.py
writes the project next to the Gerbers). The MCU module plugs into
female headers. Routing is a grid maze router (bend-penalised Dijkstra) on two
layers; through-hole pads exist on both, so no vias are needed. A GND pour fills
the bottom layer. Every step is recorded in order so the UI can replay the
board being built.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import zipfile

from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from . import kicad

P = 2.54            # pin pitch
G = 1.27            # routing grid
PAD_D, DRILL_D = 1.6, 0.9
CLEAR = 0.25
W_SIG, W_PWR = 0.4, 1.0
MARGIN = 3.81

# Real module pinouts, top view, USB at the top. (column x offset in pitches, pins top -> bottom)
MODULES = {
    "arduino_nano": {"cols": [(0, ["D13", "3V3", "AREF", "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7", "5V", "RST", "GND", "VIN"]),
                              (6, ["D12", "D11", "D10", "D9", "D8", "D7", "D6", "D5", "D4", "D3", "D2", "GND2", "RST2", "RX0", "TX1"])],
                     "servo": "D9", "sense": "A0", "vcc": "5V", "gnd": "GND", "label": "Arduino Nano"},
    "rp2040_pico": {"cols": [(0, ["GP0", "GP1", "GND", "GP2", "GP3", "GP4", "GP5", "GND2", "GP6", "GP7", "GP8", "GP9", "GND3",
                                  "GP10", "GP11", "GP12", "GP13", "GND4", "GP14", "GP15"]),
                             (7, ["VBUS", "VSYS", "GND5", "3V3_EN", "3V3", "ADC_VREF", "GP28", "AGND", "GP27", "GP26",
                                  "RUN", "GP22", "GND6", "GP21", "GP20", "GP19", "GP18", "GND7", "GP17", "GP16"])],
                    "servo": "GP15", "sense": "GP26", "vcc": "3V3", "gnd": "GND", "label": "Raspberry Pi Pico"},
    # DevKitC V4; verify the header spacing against your board before ordering a PCB
    "esp32_devkit": {"cols": [(0, ["3V3", "EN", "VP", "VN", "IO34", "IO35", "IO32", "IO33", "IO25", "IO26", "IO27", "IO14",
                                   "IO12", "GND", "IO13", "SD2", "SD3", "CMD", "5V"]),
                              (10, ["GND2", "IO23", "IO22", "TX", "RX", "IO21", "GND3", "IO19", "IO18", "IO5", "IO17",
                                    "IO16", "IO4", "IO0", "IO2", "IO15", "SD1", "SD0", "CLK"])],
                     "servo": "IO18", "sense": "IO34", "vcc": "3V3", "gnd": "GND", "label": "ESP32 DevKitC"},
}


def _components(mcu: str, divider_ohm: float):
    m = MODULES[mcu]
    comps, x0, y_top = [], MARGIN, None
    rows = max(len(c[1]) for c in m["cols"])
    y_top = MARGIN + (rows - 1) * P
    pads = []
    for dx, names in m["cols"]:
        for i, n in enumerate(names):
            pads.append({"name": n, "x": x0 + dx * P, "y": y_top - i * P})
    width = (m["cols"][1][0]) * P
    comps.append({"ref": "U1", "value": m["label"], "pads": pads,
                  "outline": [x0 - 1.6, MARGIN - 1.6, x0 + width + 1.6, y_top + 1.6]})
    xr = x0 + width + 4 * P
    top = y_top
    comps += [
        {"ref": "J3", "value": "6V in", "pads": [
            {"name": "1", "x": xr, "y": top}, {"name": "2", "x": xr + 2 * P, "y": top}],
         "outline": [xr - 2.5, top - 3.5, xr + 2 * P + 2.5, top + 3.5]},
        {"ref": "C1", "value": "470uF", "pads": [
            {"name": "+", "x": xr, "y": top - 4 * P}, {"name": "-", "x": xr + 2 * P, "y": top - 4 * P}],
         "outline": [xr - 2.5, top - 4 * P - 2.5, xr + 2 * P + 2.5, top - 4 * P + 2.5], "round": True},
        {"ref": "J1", "value": "servo", "pads": [
            {"name": "S", "x": xr, "y": top - 8 * P}, {"name": "+", "x": xr + P, "y": top - 8 * P},
            {"name": "-", "x": xr + 2 * P, "y": top - 8 * P}],
         "outline": [xr - 1.4, top - 8 * P - 1.4, xr + 2 * P + 1.4, top - 8 * P + 1.4]},
        {"ref": "R1", "value": f"{divider_ohm / 1000:g}k", "pads": [
            {"name": "1", "x": xr, "y": top - 11 * P}, {"name": "2", "x": xr + 4 * P, "y": top - 11 * P}],
         "outline": [xr + 1.6, top - 11 * P - 1.1, xr + 4 * P - 1.6, top - 11 * P + 1.1]},
        {"ref": "J2", "value": "FSR", "pads": [
            {"name": "1", "x": xr, "y": top - 14 * P}, {"name": "2", "x": xr + P, "y": top - 14 * P}],
         "outline": [xr - 1.4, top - 14 * P - 1.4, xr + P + 1.4, top - 14 * P + 1.4]},
    ]
    nets = {
        "SERVO_SIG": [("U1", m["servo"]), ("J1", "S")],
        "FSR_SENSE": [("U1", m["sense"]), ("J2", "2"), ("R1", "1")],
        "VCC_LOGIC": [("U1", m["vcc"]), ("J2", "1")],
        "V_SERVO": [("J3", "1"), ("C1", "+"), ("J1", "+")],
        "GND": [("J3", "2"), ("C1", "-"), ("J1", "-"), ("R1", "2"), ("U1", m["gnd"])],
    }
    return comps, nets


class Router:
    def __init__(self, w, h):
        self.nx, self.ny = int(math.ceil(w / G)) + 1, int(math.ceil(h / G)) + 1
        self.occ = [dict(), dict()]   # layer -> {(i, j): (net, width)}
        self.pads = []                # (x, y, net)

    def cell_xy(self, c):
        return c[0] * G, c[1] * G

    def free(self, layer, c, net, width):
        i, j = c
        if not (1 <= i < self.nx - 1 and 1 <= j < self.ny - 1):
            return False
        x, y = self.cell_xy(c)
        for px, py, pn in self.pads:
            if pn != net and math.hypot(px - x, py - y) < PAD_D / 2 + CLEAR + width / 2 - 1e-6:
                return False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                o = self.occ[layer].get((i + di, j + dj))
                if o and o[0] != net and math.hypot(di * G, dj * G) < o[1] / 2 + CLEAR + width / 2 - 1e-6:
                    return False
        return True

    def route(self, layer, sources, targets, net, width):
        """Bend-penalised Dijkstra from a set of source cells to any target cell."""
        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        pq, best, prev = [], {}, {}
        for s in sources:
            for d in range(4):
                best[(s, d)] = 0
                heapq.heappush(pq, (0, s, d))
        while pq:
            cost, c, d = heapq.heappop(pq)
            if cost > best.get((c, d), 1e18):
                continue
            if c in targets:
                path, key = [c], (c, d)
                while key in prev:
                    key = prev[key]
                    path.append(key[0])
                return path[::-1]
            for nd, (di, dj) in enumerate(dirs):
                n = (c[0] + di, c[1] + dj)
                if n not in targets and not self.free(layer, n, net, width):
                    continue
                nc = cost + 1 + (3 if nd != d else 0)
                if nc < best.get((n, nd), 1e18):
                    best[(n, nd)] = nc
                    prev[(n, nd)] = (c, d)
                    heapq.heappush(pq, (nc, n, nd))
        return None

    def commit(self, layer, path, net, width):
        for c in path:
            self.occ[layer][c] = (net, width)


def _simplify(pts):
    out = [pts[0]]
    for a, b in zip(pts[1:], pts[2:] + [None]):
        if b is None or (out[-1][0] - a[0]) * (a[1] - b[1]) != (out[-1][1] - a[1]) * (a[0] - b[0]):
            out.append(a)
    return out


def _connectivity(nets, pad_xy, traces, cell) -> list:
    """Union-find over copper: pads join both layers, traces join cells on their own layer only."""
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    pad_cells = {cell(xy) for xy in pad_xy.values()}
    for c in pad_cells:
        union(("B", c), ("F", c))
    for t in traces:
        lay = "B" if t["layer"] == "B.Cu" else "F"
        pts = [cell(p) for p in t["points"]]
        for a, b in zip(pts, pts[1:]):   # walk every grid cell along each straight segment
            steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1)
            prev = a
            for k in range(1, steps + 1):
                cur = (a[0] + (b[0] - a[0]) * k // steps, a[1] + (b[1] - a[1]) * k // steps)
                union((lay, prev), (lay, cur))
                prev = cur
    bad = []
    for net, pins in nets.items():
        roots = {find(("B", cell(pad_xy[p]))) for p in pins}
        if len(roots) > 1:
            bad.append({"net": net, "islands": len(roots)})
    return bad


def generate(ep: dict) -> dict:
    mcu = ep["mcu"]
    divider = (ep.get("sensing") or {}).get("divider_ohm", 10000)
    comps, nets = _components(mcu, divider)
    xs = [p["x"] for c in comps for p in c["pads"]] + [c["outline"][2] for c in comps]
    ys = [p["y"] for c in comps for p in c["pads"]] + [c["outline"][3] for c in comps]
    W = round((max(xs) + MARGIN) / G) * G
    H = round((max(ys) + MARGIN) / G) * G
    r = Router(W, H)
    pad_net = {}
    for net, pins in nets.items():
        for ref, name in pins:
            pad_net[(ref, name)] = net
    for c in comps:
        for p in c["pads"]:
            p["net"] = pad_net.get((c["ref"], p["name"]))
            r.pads.append((p["x"], p["y"], p["net"]))
    pad_xy = {(c["ref"], p["name"]): (p["x"], p["y"]) for c in comps for p in c["pads"]}
    cell = lambda xy: (round(xy[0] / G), round(xy[1] / G))  # noqa: E731

    traces, unrouted, steps = [], [], []
    order = sorted(nets, key=lambda n: (n == "GND", len(nets[n])))
    for net in order:
        width = W_PWR if net in ("V_SERVO", "GND") else W_SIG
        pins = [cell(pad_xy[p]) for p in nets[net]]
        tree = {pins[0]}          # connected through-hole pads: valid starting points on BOTH layers
        remaining = pins[1:]
        while remaining:
            routed = False
            for layer in (1, 0):  # bottom first, then top
                # start from connected pads or copper of this net on the SAME layer (no implicit vias)
                srcs = tree | {c for c, o in r.occ[layer].items() if o[0] == net}
                path = r.route(layer, srcs, set(remaining), net, width)
                if path:
                    r.commit(layer, path, net, width)
                    pts = _simplify([r.cell_xy(c) for c in path])
                    traces.append({"net": net, "layer": "B.Cu" if layer else "F.Cu", "width": width,
                                   "points": [[round(x, 3), round(y, 3)] for x, y in pts]})
                    end = path[-1]
                    tree.add(end)
                    remaining.remove(end)
                    routed = True
                    break
            if not routed:
                unrouted.append({"net": net, "pins": len(remaining)})
                break

    # GND pour on the bottom layer, cleared around everything that is not GND
    outline = box(0, 0, W, H)
    keep_out = []
    for c in comps:
        for p in c["pads"]:
            if p["net"] != "GND":
                keep_out.append(Point(p["x"], p["y"]).buffer(PAD_D / 2 + 0.5))
    for t in traces:
        if t["layer"] == "B.Cu" and t["net"] != "GND":
            keep_out.append(LineString(t["points"]).buffer(t["width"] / 2 + 0.5) if len(t["points"]) > 1
                            else Point(t["points"][0]).buffer(t["width"] / 2 + 0.5))
    pour = outline.buffer(-0.6, join_style=2).difference(unary_union(keep_out))
    gnd_pads = [Point(p["x"], p["y"]) for c in comps for p in c["pads"] if p["net"] == "GND"]
    polys = [pour] if pour.geom_type == "Polygon" else list(pour.geoms)
    polys = [g for g in polys if any(g.intersects(gp) for gp in gnd_pads)]  # drop islands
    pour_rings = [{"exterior": [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords],
                   "holes": [[[round(x, 3), round(y, 3)] for x, y in h.coords] for h in g.interiors]} for g in polys]

    ratsnest = []
    for net, pins in nets.items():
        pts = [pad_xy[p] for p in pins]
        for a, b in zip(pts, pts[1:]):
            ratsnest.append({"net": net, "a": a, "b": b})

    disconnected = _connectivity(nets, pad_xy, traces, cell)
    drc = {"unrouted": unrouted, "disconnected": disconnected, "clearance_mm": CLEAR, "min_trace_mm": W_SIG,
           "pass": not unrouted and not disconnected}
    return {"size_mm": [round(W, 2), round(H, 2)], "components": comps, "nets": {k: [f"{a}.{b}" for a, b in v] for k, v in nets.items()},
            "traces": traces, "pour": pour_rings, "ratsnest": ratsnest, "drc": drc,
            "notes": ["Plug the MCU into female headers; power it from USB for serial/bench.",
                      "Hand-solder on 0.1\" perfboard following this layout, or order the Gerbers (2 layers, 1.6 mm)."]}


# ---------------------------------------------------------------- outputs

def _gerber_header(name):
    return ["%FSLAX46Y46*%", "%MOMM*%", f"G04 Morph carrier board {name}*", "%LPD*%"]


def _fmt(v):
    return f"{int(round(v * 1e6))}"


def write_gerbers(board: dict, outdir: str) -> list:
    os.makedirs(outdir, exist_ok=True)
    W, H = board["size_mm"]
    files = {}
    for layer in ("F.Cu", "B.Cu"):
        lines = _gerber_header(layer)
        lines += [f"%ADD10C,{PAD_D:.3f}*%", f"%ADD11C,{W_SIG:.3f}*%", f"%ADD12C,{W_PWR:.3f}*%"]
        if layer == "B.Cu":
            for ring in board["pour"]:
                lines += ["G36*"] + [f"X{_fmt(x)}Y{_fmt(y)}D0{2 if i == 0 else 1}*" for i, (x, y) in enumerate(ring["exterior"])] + ["G37*"]
                lines.append("%LPC*%")
                for hole in ring["holes"]:
                    lines += ["G36*"] + [f"X{_fmt(x)}Y{_fmt(y)}D0{2 if i == 0 else 1}*" for i, (x, y) in enumerate(hole)] + ["G37*"]
                lines.append("%LPD*%")
        for t in board["traces"]:
            if t["layer"] != layer:
                continue
            lines.append("D12*" if t["width"] == W_PWR else "D11*")
            for i, (x, y) in enumerate(t["points"]):
                lines.append(f"X{_fmt(x)}Y{_fmt(y)}D0{2 if i == 0 else 1}*")
        lines.append("D10*")
        for c in board["components"]:
            for p in c["pads"]:
                lines.append(f"X{_fmt(p['x'])}Y{_fmt(p['y'])}D03*")
        lines.append("M02*")
        files[f"board-{layer.replace('.', '_')}.gbr"] = lines
    edge = _gerber_header("Edge.Cuts") + ["%ADD10C,0.100*%", "D10*"]
    for i, (x, y) in enumerate([(0, 0), (W, 0), (W, H), (0, H), (0, 0)]):
        edge.append(f"X{_fmt(x)}Y{_fmt(y)}D0{2 if i == 0 else 1}*")
    files["board-Edge_Cuts.gbr"] = edge + ["M02*"]
    silk = _gerber_header("F.SilkS") + ["%ADD10C,0.150*%", "D10*"]
    for c in board["components"]:
        x0, y0, x1, y1 = c["outline"]
        for i, (x, y) in enumerate([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]):
            silk.append(f"X{_fmt(x)}Y{_fmt(y)}D0{2 if i == 0 else 1}*")
    files["board-F_SilkS.gbr"] = silk + ["M02*"]
    drill = ["M48", "METRIC,TZ", f"T1C{DRILL_D:.3f}", "%", "G90", "G05", "T1"]
    for c in board["components"]:
        for p in c["pads"]:
            drill.append(f"X{p['x']:.3f}Y{p['y']:.3f}")
    drill.append("M30")
    files["board.drl"] = drill
    written = []
    for name, lines in files.items():
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        written.append(name)
    with zipfile.ZipFile(os.path.join(outdir, "gerbers.zip"), "w") as z:
        for name in written:
            z.write(os.path.join(outdir, name), name)
    return written + ["gerbers.zip"]


NET_COLORS = {"GND": "#7f8c8d", "V_SERVO": "#e74c3c", "VCC_LOGIC": "#f39c12", "SERVO_SIG": "#3498db", "FSR_SENSE": "#2ecc71"}


def to_svg(board: dict, scale: float = 10.0) -> str:
    W, H = board["size_mm"]
    fy = lambda y: H - y  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 {W + 4} {H + 4}" width="{(W + 4) * scale:.0f}" '
           f'height="{(H + 4) * scale:.0f}"><rect x="0" y="0" width="{W}" height="{H}" rx="1" fill="#0b3d2e" stroke="#d4c06b" stroke-width="0.15"/>']
    for ring in board["pour"]:
        d = "M" + " L".join(f"{x:.2f},{fy(y):.2f}" for x, y in ring["exterior"]) + " Z"
        d += "".join(" M" + " L".join(f"{x:.2f},{fy(y):.2f}" for x, y in h) + " Z" for h in ring["holes"])
        out.append(f'<path d="{d}" fill="#2c6fbb" fill-opacity=".35" fill-rule="evenodd"/>')
    for t in board["traces"]:
        pts = " ".join(f"{x:.2f},{fy(y):.2f}" for x, y in t["points"])
        col = "#4aa3ff" if t["layer"] == "B.Cu" else "#ff5a4f"
        out.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="{t["width"]}" stroke-linecap="round" stroke-linejoin="round"/>')
    for c in board["components"]:
        x0, y0, x1, y1 = c["outline"]
        out.append(f'<rect x="{x0:.2f}" y="{fy(y1):.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" fill="none" stroke="#f5f5f5" stroke-width="0.15"/>')
        out.append(f'<text x="{x0:.2f}" y="{fy(y1) - 0.5:.2f}" font-size="1.4" fill="#f5f5f5" font-family="monospace">{c["ref"]} {c["value"]}</text>')
        for p in c["pads"]:
            out.append(f'<circle cx="{p["x"]:.2f}" cy="{fy(p["y"]):.2f}" r="{PAD_D / 2}" fill="#d4af37"/>'
                       f'<circle cx="{p["x"]:.2f}" cy="{fy(p["y"]):.2f}" r="{DRILL_D / 2}" fill="#0b3d2e"/>')
    out.append("</svg>")
    return "".join(out)


def build(ep: dict, outdir: str) -> dict:
    board = generate(ep)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "board.json"), "w", encoding="utf-8") as fh:
        json.dump(board, fh)
    with open(os.path.join(outdir, "board.svg"), "w", encoding="utf-8") as fh:
        fh.write(to_svg(board))
    files = ["board.json", "board.svg"] + [f"gerbers/{f}" for f in write_gerbers(board, os.path.join(outdir, "gerbers"))]
    try:  # KiCad project + BoardRepo-ready zip; a writer bug must not cost the Gerbers
        kc = kicad.export(board, os.path.join(outdir, "kicad"), "board", {"MCU": MODULES[ep["mcu"]]["label"]})
        files += [f"kicad/{f}" for f in kc["files"] + [kc["zip"]]]
    except Exception as exc:
        kc = {"error": f"{exc.__class__.__name__}: {exc}"}
    return {"size_mm": board["size_mm"], "traces": len(board["traces"]), "drc": board["drc"], "kicad": kc, "files": files}
