"""Netlist model + schematic renderer (SVG + JSON) for the robot electronics.

The netlist model (`Circuit`, `Comp`, `Pin`) is shared by `electronics2` (which
builds circuits), `pcb` (which places and routes the on-board part of a circuit)
and the renderer below, which draws a readable, KiCad-style schematic sheet:

* functional blocks in titled frames (power, regulators, MCU, bus, sensors ...),
* box symbols with pin numbers on the stubs and pin names inside the body,
* real two-terminal / MOSFET symbols for passives,
* power symbols for supply rails and GND, global-label flags for signals,
  no-connect crosses on unused pins,
* an A3/A2 sheet with a border, zone references and a title block.

Everything is in millimetres on a 1.27 mm grid (y down, SVG convention).
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field

# ------------------------------------------------------------------ netlist model

POWER_NETS = ("GND", "3V3", "5V", "VBUS", "VSYS", "V_IN", "V_SERVO", "VIN_RAW", "V_FUSED", "V_LOGIC")


def is_power(net: str | None) -> bool:
    return bool(net) and (net in POWER_NETS or net.startswith("+"))


@dataclass
class Pin:
    num: str
    name: str
    net: str | None = None
    side: str = ""            # L | R | T | B on a box symbol ("" = auto)
    etype: str = "passive"    # power_in | power_out | input | output | bidir | passive | nc


@dataclass
class Comp:
    ref: str
    value: str
    kind: str                 # box | conn | R | C | CP | L | D | DS | TVS | LED | F | SW | QP | QN | MH
    pins: list
    footprint: str | None = None
    mpn: str = ""
    lcsc: str = ""            # LCSC part number, or "verify"
    price: float = 0.0        # unit price USD (qty ~10)
    desc: str = ""
    block: str = ""
    board: bool = True        # lives on the PCB (False = off-board device in a wiring diagram)
    place: dict = field(default_factory=dict)   # placement hints for pcb.py
    note: str = ""            # shown under the symbol (e.g. "to servo ID 1-2")
    meta: dict = field(default_factory=dict)

    def pin(self, key: str) -> Pin:
        for p in self.pins:
            if p.num == key:
                return p
        for p in self.pins:
            if p.name == key:
                return p
        raise KeyError(f"{self.ref}: no pin {key}")

    def node(self, p: Pin) -> str:
        names = [q.name for q in self.pins]
        return f"{self.ref}.{p.name}" if names.count(p.name) == 1 else f"{self.ref}.{p.num}"


class Circuit:
    def __init__(self, title: str, subtitle: str = "", blocks: list | None = None):
        self.title = title
        self.subtitle = subtitle
        self.comps: list[Comp] = []
        self.blocks: list = list(blocks or [])     # [(key, title), ...] in drawing order
        self.notes: list = []
        self._n: dict = {}

    def ref(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}{self._n[prefix]}"

    def add(self, ref: str, value: str, kind: str, pins: list, **kw) -> Comp:
        ps = []
        for p in pins:
            if isinstance(p, Pin):
                ps.append(p)
            else:
                num, name, net, *rest = p
                ps.append(Pin(str(num), str(name), net, *(rest or [])))
        if ref.endswith("?"):
            ref = self.ref(ref[:-1])
        if any(c.ref == ref for c in self.comps):
            raise ValueError(f"duplicate ref {ref}")
        c = Comp(ref, value, kind, ps, **kw)
        self.comps.append(c)
        return c

    def comp(self, ref: str) -> Comp:
        return next(c for c in self.comps if c.ref == ref)

    def nets(self, board_only: bool = False) -> dict:
        out: dict = {}
        for c in self.comps:
            if board_only and not c.board:
                continue
            for p in c.pins:
                if p.net:
                    out.setdefault(p.net, []).append((c, p))
        return out

    def netlist(self, board_only: bool = False) -> list:
        nets = self.nets(board_only)
        order = sorted(nets, key=lambda n: (n != "GND", not is_power(n), n))
        return [{"name": n, "nodes": [c.node(p) for c, p in nets[n]]} for n in order]

    def to_json(self) -> dict:
        return {"title": self.title, "subtitle": self.subtitle, "blocks": [list(b) for b in self.blocks],
                "components": [{"ref": c.ref, "value": c.value, "kind": c.kind, "block": c.block, "mpn": c.mpn,
                                "lcsc": c.lcsc, "footprint": c.footprint, "board": c.board, "desc": c.desc,
                                "pins": [{"num": p.num, "name": p.name, "net": p.net, "type": p.etype} for p in c.pins]}
                               for c in self.comps],
                "nets": self.netlist(), "notes": self.notes}


# ------------------------------------------------------------------ renderer
G = 2.54                    # pin pitch / symbol grid
PIN_LEN = 2.54
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
C = {   # KiCad-like default theme
    "paper": "#f5f4ef", "frame": "#840000", "body": "#840000", "fill": "#ffffc2", "pin": "#840000", "pinnum": "#a90000",
    "pinname": "#006464", "wire": "#008400", "label": "#840000", "labeltext": "#1a1a1a", "ref": "#006464",
    "value": "#006464", "power": "#840000", "nc": "#0000c8", "note": "#1f3a93", "block": "#7a8494", "blockfill": "#efeee6",
    "junction": "#008400"}
SHEETS = [("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0), ("A1", 841.0, 594.0)]
CW = 0.62    # average glyph advance / font size


def _tw(text: str, size: float) -> float:
    return len(text) * size * CW


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _t(x, y, s, size, color, anchor="start", weight="400", rot=0, italic=False, extra=""):
    tr = f' transform="rotate({rot} {x:.3f} {y:.3f})"' if rot else ""
    st = ' font-style="italic"' if italic else ""
    return (f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size:.2f}" fill="{color}" text-anchor="{anchor}" '
            f'font-weight="{weight}"{st}{tr}{extra}>{_e(s)}</text>')


def _l(x0, y0, x1, y1, color, w=0.15):
    return f'<line x1="{x0:.3f}" y1="{y0:.3f}" x2="{x1:.3f}" y2="{y1:.3f}" stroke="{color}" stroke-width="{w}" stroke-linecap="round"/>'


class _El:
    """A drawable placed by the block packer: svg in local coords, bbox (0, 0, w, h)."""

    def __init__(self, svg: str, w: float, h: float, ref: str = "", pins: dict | None = None):
        self.svg, self.w, self.h, self.ref = svg, w, h, ref
        self.pins = pins or {}      # pin node -> (x, y) local


def _power_symbol(x, y, net, up=True) -> str:
    """Power port with its connection point at (x, y): GND points down, rails point up."""
    col = C["power"]
    if net == "GND":
        return (_l(x, y, x, y + 1.27, col) + _l(x - 1.27, y + 1.27, x + 1.27, y + 1.27, col, 0.2) +
                _l(x - 0.8, y + 1.75, x + 0.8, y + 1.75, col, 0.2) + _l(x - 0.35, y + 2.2, x + 0.35, y + 2.2, col, 0.2))
    label = {"3V3": "+3V3", "5V": "+5V"}.get(net, net)
    if up:
        return (_l(x, y, x, y - 1.27, col) + _l(x - 0.76, y - 1.27, x + 0.76, y - 1.27, col, 0.2) +
                f'<path d="M{x - 0.76:.3f},{y - 1.27:.3f} L{x:.3f},{y - 2.1:.3f} L{x + 0.76:.3f},{y - 1.27:.3f}" fill="none" stroke="{col}" stroke-width="0.15"/>' +
                _t(x, y - 2.7, label, 1.27, C["value"], "middle"))
    return _l(x, y, x, y + 1.27, col) + _l(x - 0.76, y + 1.27, x + 0.76, y + 1.27, col, 0.2) + _t(x, y + 2.9, label, 1.27, C["value"], "middle")


def _glabel(x, y, net, direction="right") -> tuple[str, float]:
    """Global label flag; connection point (x, y); returns (svg, width)."""
    size = 1.27
    tw = _tw(net, size)
    h = 1.9
    w = tw + 2.2
    bold = "700" if is_power(net) else "400"
    fill = "#fff6e0" if is_power(net) else "#ffffff"
    if direction == "right":
        pts = [(x, y), (x + 0.95, y - h / 2), (x + w, y - h / 2), (x + w, y + h / 2), (x + 0.95, y + h / 2)]
        tx, anchor = x + 1.3, "start"
    else:
        pts = [(x, y), (x - 0.95, y - h / 2), (x - w, y - h / 2), (x - w, y + h / 2), (x - 0.95, y + h / 2)]
        tx, anchor = x - 1.3, "end"
    d = "M" + " L".join(f"{a:.3f},{b:.3f}" for a, b in pts) + " Z"
    return (f'<path d="{d}" fill="{fill}" stroke="{C["label"]}" stroke-width="0.15"/>' +
            _t(tx, y + size * 0.36, net, size, C["labeltext"], anchor, bold)), w


def _nc(x, y) -> str:
    s = 0.6
    return _l(x - s, y - s, x + s, y + s, C["nc"], 0.18) + _l(x - s, y + s, x + s, y - s, C["nc"], 0.18)


def _attach_h(x, y, net, direction):
    """Horizontal attachment at a pin end: wire + label, or no-connect. Returns (svg, extent)."""
    if not net:
        return _nc(x, y), 0.8
    sgn = 1 if direction == "right" else -1
    wx = x + sgn * G
    lab, w = _glabel(wx, y, net, direction)
    return _l(x, y, wx, y, C["wire"], 0.18) + lab, G + w


def _box_symbol(comp: Comp) -> _El:
    sides = {"L": [], "R": [], "T": [], "B": []}
    for p in comp.pins:
        sd = p.side
        if not sd:
            if p.net == "GND" or p.name.startswith("GND"):
                sd = "B"
            elif p.etype in ("power_in",) and is_power(p.net):
                sd = "T"
            elif p.etype in ("output",):
                sd = "R"
            else:
                sd = "L" if comp.kind != "conn" else "R"
        sides[sd].append(p)
    ns = 1.27
    nameL = max([_tw(p.name, ns) for p in sides["L"]] + [0])
    nameR = max([_tw(p.name, ns) for p in sides["R"]] + [0])
    bw = max(nameL + nameR + 4.0, (max(len(sides["T"]), len(sides["B"])) + 1) * G, 10.16)
    bw = math.ceil(bw / G) * G
    rows = max(len(sides["L"]), len(sides["R"]), 1)
    bh = (rows + 1) * G
    if sides["T"] and sides["B"]:
        bh = max(bh, 3 * G)
    ext = {}
    parts = {}
    for sd in ("L", "R"):
        e, svg = 0.0, []
        for p in sides[sd]:
            _, w = _attach_h(0, 0, p.net, "left" if sd == "L" else "right")
            e = max(e, w)
        ext[sd] = PIN_LEN + e
    top_lab = max([(_tw(p.net, 1.27) + 2.2 + G) if (p.net and not (is_power(p.net) and p.net != "GND")) else 4.5
                   for p in sides["T"]] + [0])
    ext["T"] = PIN_LEN + top_lab + 3.2          # + ref/value text
    ext["B"] = PIN_LEN + (3.2 if sides["B"] else 0) + (2.2 if comp.note else 0) + 1.0
    if not sides["T"]:
        ext["T"] = 4.2
    ox, oy = ext["L"], ext["T"]
    body = (f'<rect x="{ox:.3f}" y="{oy:.3f}" width="{bw:.3f}" height="{bh:.3f}" fill="{C["fill"]}" '
            f'stroke="{C["body"]}" stroke-width="0.25"/>')
    s = [body]
    pins = {}
    for sd in ("L", "R"):
        for i, p in enumerate(sides[sd]):
            y = oy + G * (i + 1)
            if sd == "L":
                x0, x1 = ox, ox - PIN_LEN
                s.append(_l(x0, y, x1, y, C["pin"], 0.18))
                s.append(_t(x0 + 0.7, y + 0.45, p.name, ns, C["pinname"]))
                s.append(_t((x0 + x1) / 2, y - 0.45, p.num.split("-")[-1], 1.0, C["pinnum"], "middle"))
                a, _ = _attach_h(x1, y, p.net, "left")
            else:
                x0, x1 = ox + bw, ox + bw + PIN_LEN
                s.append(_l(x0, y, x1, y, C["pin"], 0.18))
                s.append(_t(x0 - 0.7, y + 0.45, p.name, ns, C["pinname"], "end"))
                s.append(_t((x0 + x1) / 2, y - 0.45, p.num.split("-")[-1], 1.0, C["pinnum"], "middle"))
                a, _ = _attach_h(x1, y, p.net, "right")
            s.append(a)
            s.append(f'<circle cx="{x1:.3f}" cy="{y:.3f}" r="0.25" fill="{C["pin"]}" opacity="0"/>')
            pins[comp.node(p)] = (x1, y)
    for sd in ("T", "B"):
        ps = sides[sd]
        for i, p in enumerate(ps):
            x = ox + bw * (i + 1) / (len(ps) + 1)
            x = round(x / 1.27) * 1.27 if len(ps) > 1 else ox + bw / 2
            if sd == "T":
                y0, y1 = oy, oy - PIN_LEN
                s.append(_l(x, y0, x, y1, C["pin"], 0.18))
                s.append(_t(x, y0 + 1.7, p.name, ns, C["pinname"], "middle"))
                s.append(_t(x - 0.45, (y0 + y1) / 2 + 0.35, p.num, 1.0, C["pinnum"], "end"))
                s.append(_power_symbol(x, y1, p.net, up=True) if is_power(p.net) and p.net != "GND" else
                         (_nc(x, y1) if not p.net else _glabel_v(x, y1, p.net, up=True)))
            else:
                y0, y1 = oy + bh, oy + bh + PIN_LEN
                s.append(_l(x, y0, x, y1, C["pin"], 0.18))
                s.append(_t(x, y0 - 0.8, p.name, ns, C["pinname"], "middle"))
                s.append(_t(x - 0.45, (y0 + y1) / 2 + 0.35, p.num, 1.0, C["pinnum"], "end"))
                s.append(_power_symbol(x, y1, p.net, up=False) if p.net == "GND" else
                         (_nc(x, y1) if not p.net else _glabel_v(x, y1, p.net, up=False)))
            pins[comp.node(p)] = (x, y1)
    s.append(_t(ox, oy - (ext["T"] - 3.0 if sides["T"] else 2.4) - (0 if not sides["T"] else 0), comp.ref, 1.5, C["ref"], weight="700")
             if not sides["T"] else _t(ox, 1.6, comp.ref, 1.5, C["ref"], weight="700"))
    vx = ox + _tw(comp.ref, 1.5) + 1.2
    s.append(_t(vx, (oy - 2.4) if not sides["T"] else 1.6, comp.value, 1.4, C["value"]))
    if comp.note:
        s.append(_t(ox, oy + bh + (PIN_LEN + 3.6 if sides["B"] else 2.3), comp.note, 1.15, C["note"], italic=True))
    W = ext["L"] + bw + ext["R"]
    Hh = ext["T"] + bh + ext["B"]
    W = max(W, ext["L"] + _tw(comp.note, 1.15) + 1, ext["L"] + _tw(comp.ref + comp.value, 1.5) + 3)
    return _El("".join(s), W, Hh, comp.ref, pins)


def _glabel_v(x, y, net, up=True) -> str:
    """Vertical net label (rotated flag) for top/bottom pins."""
    svg, w = _glabel(0, 0, net, "right")
    rot = -90 if up else 90
    return f'<g transform="translate({x:.3f} {y:.3f}) rotate({rot})">{_l(0, 0, G, 0, C["wire"], 0.18)}<g transform="translate({G} 0)">{svg}</g></g>'


def _two_pin_body(kind, cx, top, bot, value="", flip=False) -> str:
    """Symbol body between y=top and y=bot at x=cx (pin 1 at the top unless flip)."""
    col = C["body"]
    mid = (top + bot) / 2
    s = []
    if kind == "R":
        s.append(_l(cx, top, cx, mid - 1.4, col, 0.18) + _l(cx, mid + 1.4, cx, bot, col, 0.18))
        s.append(f'<rect x="{cx - 0.5:.3f}" y="{mid - 1.4:.3f}" width="1.0" height="2.8" fill="none" stroke="{col}" stroke-width="0.25"/>')
    elif kind in ("C", "CP"):
        s.append(_l(cx, top, cx, mid - 0.35, col, 0.18) + _l(cx, mid + 0.35, cx, bot, col, 0.18))
        s.append(_l(cx - 1.1, mid - 0.35, cx + 1.1, mid - 0.35, col, 0.3))
        if kind == "CP":
            s.append(f'<path d="M{cx - 1.1:.3f},{mid + 0.75:.3f} Q{cx:.3f},{mid + 0.1:.3f} {cx + 1.1:.3f},{mid + 0.75:.3f}" fill="none" stroke="{col}" stroke-width="0.3"/>')
            s.append(_t(cx - 1.5, mid - 0.8, "+", 1.2, col, "middle"))
        else:
            s.append(_l(cx - 1.1, mid + 0.35, cx + 1.1, mid + 0.35, col, 0.3))
    elif kind == "L":
        s.append(_l(cx, top, cx, mid - 1.6, col, 0.18) + _l(cx, mid + 1.6, cx, bot, col, 0.18))
        d = f"M{cx:.3f},{mid - 1.6:.3f}" + "".join(f" a0.4,0.4 0 0 1 0,0.8" for _ in range(4))
        s.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="0.25"/>')
    elif kind == "F":
        s.append(_l(cx, top, cx, bot, col, 0.18))
        s.append(f'<rect x="{cx - 0.6:.3f}" y="{mid - 1.5:.3f}" width="1.2" height="3.0" fill="{C["paper"]}" stroke="{col}" stroke-width="0.25"/>')
        s.append(_l(cx, mid - 1.5, cx, mid + 1.5, col, 0.18))
    elif kind == "SW":
        s.append(_l(cx, top, cx, mid - 1.0, col, 0.18) + _l(cx, mid + 1.0, cx, bot, col, 0.18))
        s.append(f'<circle cx="{cx:.3f}" cy="{mid - 1.0:.3f}" r="0.3" fill="none" stroke="{col}" stroke-width="0.18"/>')
        s.append(f'<circle cx="{cx:.3f}" cy="{mid + 1.0:.3f}" r="0.3" fill="none" stroke="{col}" stroke-width="0.18"/>')
        s.append(_l(cx - 1.0, mid - 1.2, cx - 1.0, mid + 1.2, col, 0.25) + _l(cx - 1.0, mid, cx - 2.0, mid, col, 0.25))
    elif kind in ("D", "DS", "LED", "TVS", "DZ"):
        # anode at the top, cathode at the bottom unless flip (then cathode at the top)
        s.append(_l(cx, top, cx, mid - 0.9, col, 0.18) + _l(cx, mid + 0.9, cx, bot, col, 0.18))
        if not flip:     # triangle pointing down (A top -> K bottom)
            tri = f"M{cx - 1.0:.3f},{mid - 0.9:.3f} L{cx + 1.0:.3f},{mid - 0.9:.3f} L{cx:.3f},{mid + 0.8:.3f} Z"
            by = mid + 0.85
        else:
            tri = f"M{cx - 1.0:.3f},{mid + 0.9:.3f} L{cx + 1.0:.3f},{mid + 0.9:.3f} L{cx:.3f},{mid - 0.8:.3f} Z"
            by = mid - 0.85
        s.append(f'<path d="{tri}" fill="{C["fill"] if kind != "LED" else "#ffe9b0"}" stroke="{col}" stroke-width="0.22"/>')
        s.append(_l(cx - 1.0, by, cx + 1.0, by, col, 0.3))
        if kind == "DS":
            s.append(_l(cx - 1.0, by, cx - 1.0, by + (0.35 if not flip else -0.35), col, 0.22) +
                     _l(cx + 1.0, by, cx + 1.0, by - (0.35 if not flip else -0.35), col, 0.22))
        if kind in ("DZ",):
            s.append(_l(cx + 1.0, by, cx + 1.3, by + (0.4 if not flip else -0.4), col, 0.22))
        if kind == "TVS":
            tri2 = (f"M{cx - 1.0:.3f},{by + 1.7:.3f} L{cx + 1.0:.3f},{by + 1.7:.3f} L{cx:.3f},{by:.3f} Z" if not flip else
                    f"M{cx - 1.0:.3f},{by - 1.7:.3f} L{cx + 1.0:.3f},{by - 1.7:.3f} L{cx:.3f},{by:.3f} Z")
            s.append(f'<path d="{tri2}" fill="none" stroke="{col}" stroke-width="0.0"/>')
            s.append(_l(cx - 1.0, by, cx - 1.3, by - (0.4 if not flip else -0.4), col, 0.22))
        if kind == "LED":
            for dy in (-0.2, 0.5):
                s.append(f'<path d="M{cx + 1.3:.3f},{mid + dy:.3f} l1.0,-0.7 m-0.45,0 l0.45,0 l0,0.4" fill="none" stroke="{col}" stroke-width="0.15"/>')
    return "".join(s)


def _mosfet(comp: Comp) -> _El:
    """3-pin MOSFET: gate left, drain/source top/bottom (P-ch: source on top)."""
    pg, pd, ps = comp.pin("G"), comp.pin("D"), comp.pin("S")
    pch = comp.kind == "QP"
    top_p, bot_p = (ps, pd) if pch else (pd, ps)
    col = C["body"]
    left = 2 * G + max(_tw(pg.net or "", 1.27) + 2.4, 2.0)
    cx = left + 2.2
    top, bot = 4.4, 4.4 + 4 * G
    mid = (top + bot) / 2
    s = []
    s.append(_l(cx, top, cx, mid - 1.6, col, 0.18) + _l(cx, mid + 1.6, cx, bot, col, 0.18))
    s.append(_l(cx, mid - 1.6, cx - 1.2, mid - 1.6, col, 0.2) + _l(cx, mid + 1.6, cx - 1.2, mid + 1.6, col, 0.2))
    for dy in (-1.6, 0.0, 1.6):
        s.append(_l(cx - 1.2, mid + dy - 0.55, cx - 1.2, mid + dy + 0.55, col, 0.3))
    s.append(_l(cx - 1.2, mid, cx, mid, col, 0.2) + _l(cx, mid, cx, mid + (-1.6 if pch else 1.6), col, 0.2))
    s.append(_l(cx - 1.9, mid - 1.6, cx - 1.9, mid + 1.6, col, 0.3))       # gate plate
    gx = cx - 1.9
    s.append(_l(gx, mid + (1.6 if pch else 1.6), gx - 2 * G + 2.2, mid + 1.6, col, 0.18))
    arrow = (f"M{cx - 0.4:.3f},{mid - 0.35:.3f} L{cx - 1.1:.3f},{mid:.3f} L{cx - 0.4:.3f},{mid + 0.35:.3f} Z" if not pch else
             f"M{cx - 0.9:.3f},{mid - 0.35:.3f} L{cx - 0.2:.3f},{mid:.3f} L{cx - 0.9:.3f},{mid + 0.35:.3f} Z")
    s.append(f'<path d="{arrow}" fill="{col}"/>')
    s.append(f'<circle cx="{cx - 1.0:.3f}" cy="{mid:.3f}" r="2.7" fill="none" stroke="{col}" stroke-width="0.18"/>')
    gend = gx - 2 * G + 2.2
    lab, _ = _attach_h(gend, mid + 1.6, pg.net, "left")
    s.append(lab)
    s.append(_t(gend + 0.3, mid + 1.15, pg.num, 1.0, C["pinnum"]))
    pins = {comp.node(pg): (gend, mid + 1.6)}
    for p, y, up in ((top_p, top, True), (bot_p, bot, False)):
        s.append(_t(cx + 0.4, y + (1.0 if up else -0.4), p.num, 1.0, C["pinnum"]))
        a, _ = _end_attach(cx, y, p.net, up)
        s.append(a)
        pins[comp.node(p)] = (cx, y)
    tx = cx + 3.2
    s.append(_t(tx, mid - 0.6, comp.ref, 1.5, C["ref"], weight="700"))
    s.append(_t(tx, mid + 1.3, comp.value, 1.3, C["value"]))
    if comp.note:
        s.append(_t(tx, mid + 3.0, comp.note, 1.1, C["note"], italic=True))
    lw = max(_tw(top_p.net or "", 1.27), _tw(bot_p.net or "", 1.27)) + 2.6
    w = max(tx + max(_tw(comp.value, 1.3), _tw(comp.note, 1.1), _tw(comp.ref, 1.5)) + 1.0, cx + lw)
    h = bot + 3.6
    return _El("".join(s), w, h, comp.ref, pins)


def _shift(el: _El, dx, dy) -> _El:
    return _El(f'<g transform="translate({dx:.3f} {dy:.3f})">{el.svg}</g>', el.w + dx, el.h + dy, el.ref,
               {k: (x + dx, y + dy) for k, (x, y) in el.pins.items()})


def _end_attach(cx, y, net, up):
    """Terminal of a vertical two-pin symbol: power port, or a short wire + horizontal label to the right."""
    if is_power(net) and not (net == "GND" and up):
        return _power_symbol(cx, y, net, up=up), 0.0
    if not net:
        return _nc(cx, y), 0.0
    wy = y - 1.27 if up else y + 1.27
    lab, w = _glabel(cx, wy, net, "right")
    return _l(cx, y, cx, wy, C["wire"], 0.18) + f'<circle cx="{cx:.3f}" cy="{wy:.3f}" r="0.0"/>' + lab, w


def _two_pin(comp: Comp) -> _El:
    p1, p2 = comp.pins[0], comp.pins[1]
    diode = comp.kind in ("D", "DS", "LED", "TVS", "DZ")
    top_p, bot_p = p1, p2
    if p1.net == "GND" or (is_power(p2.net) and not is_power(p1.net) and p2.net != "GND"):
        top_p, bot_p = p2, p1
    flip = diode and top_p.name == "K"
    def head(p):
        if not p.net:
            return 1.2
        if is_power(p.net) and p.net != "GND":
            return 3.8
        return 2.6
    cx = 1.6
    top = head(top_p)
    bot = top + 3 * G
    s = [_two_pin_body(comp.kind, cx, top, bot, comp.value, flip)]
    pins, lw = {}, 0.0
    for p, y, up in ((top_p, top, True), (bot_p, bot, False)):
        a, w = _end_attach(cx, y, p.net, up)
        s.append(a)
        lw = max(lw, w)
        pins[comp.node(p)] = (cx, y)
        s.append(_t(cx - 0.45, y + (1.15 if up else -0.45), p.name if diode else p.num, 0.9, C["pinnum"], "end"))
    tx = cx + (3.4 if comp.kind in ("LED", "SW") else 2.0)
    mid = (top + bot) / 2
    s.append(_t(tx, mid - 0.2, comp.ref, 1.4, C["ref"], weight="700"))
    s.append(_t(tx, mid + 1.5, comp.value, 1.25, C["value"]))
    w = max(tx + max(_tw(comp.ref, 1.4), _tw(comp.value, 1.25)) + 0.6, cx + lw + 0.4)
    h = bot + (3.4 if bot_p.net == "GND" else 2.6 if bot_p.net else 1.2)
    return _El("".join(s), w, h, comp.ref, pins)


def _mount_hole(comp: Comp) -> _El:
    s = (f'<circle cx="2.4" cy="2.4" r="1.6" fill="none" stroke="{C["body"]}" stroke-width="0.25"/>'
         f'<circle cx="2.4" cy="2.4" r="0.8" fill="none" stroke="{C["body"]}" stroke-width="0.18"/>'
         + _t(4.6, 2.0, comp.ref, 1.3, C["ref"], weight="700") + _t(4.6, 3.6, comp.value, 1.1, C["value"]))
    return _El(s, 4.6 + max(_tw(comp.ref, 1.3), _tw(comp.value, 1.1)) + 0.5, 4.8, comp.ref)


def symbol(comp: Comp) -> _El:
    if comp.kind in ("box", "conn"):
        return _box_symbol(comp)
    if comp.kind in ("QP", "QN"):
        return _mosfet(comp)
    if comp.kind == "MH":
        return _mount_hole(comp)
    return _two_pin(comp)


def _pack(els: list, max_w: float, gap_x=5.08, gap_y=5.08) -> tuple[list, float, float]:
    """Shelf-pack elements left-to-right; returns [(el, x, y)], width, height."""
    out, x, y, row_h, W = [], 0.0, 0.0, 0.0, 0.0
    for el in els:
        if x > 0 and x + el.w > max_w:
            x, y = 0.0, y + row_h + gap_y
            row_h = 0.0
        out.append((el, x, y))
        x += el.w + gap_x
        row_h = max(row_h, el.h)
        W = max(W, x - gap_x)
    return out, W, y + row_h


def _block_layout(comps: list, max_w: float):
    big = [symbol(c) for c in comps if c.kind in ("box", "conn")]
    small = [symbol(c) for c in comps if c.kind not in ("box", "conn")]
    big.sort(key=lambda e: -e.h)
    pb, wb, hb = _pack(big, max_w, 7.62, 6.0)
    ps, ws, hs = _pack(small, max(max_w, wb), 3.81, 4.0)
    placed = list(pb)
    oy = hb + (5.0 if big and small else 0.0)
    placed += [(e, x, y + oy) for e, x, y in ps]
    return placed, max(wb, ws), oy + hs


def _skyline(laid, W, gap=5.0, limit=None):
    """Bottom-left skyline packing of block frames, in order: [(key, title, placed, x, y, w, h)].
    `limit(x0, x1)` is the lowest y a frame spanning x0..x1 may reach (title block / notes are obstacles)."""
    sky = [(0.0, W, 0.0)]
    out = []
    for key, title, placed, bw, bh in laid:
        best = None
        for x in sorted({seg[0] for seg in sky}):
            if x + bw > W + 1e-6:
                continue
            y = max(seg[2] for seg in sky if seg[0] < x + bw - 1e-6 and seg[1] > x + 1e-6)
            over = max(0.0, y + bh - limit(x, x + bw)) if limit else 0.0
            if best is None or (over, y) < (best[2], best[0] - 1e-6):
                best = (y, x, over)
        if best is None:
            best = (max(seg[2] for seg in sky), 0.0, 1e9)
        y, x = best[0], best[1]
        out.append((key, title, placed, x, y, bw, bh, best[2]))
        x1 = min(x + bw + gap, W)
        new = []
        for a, b, yy in sky:
            if b <= x + 1e-9 or a >= x1 - 1e-9:
                new.append((a, b, yy))
                continue
            if a < x:
                new.append((a, x, yy))
            if b > x1:
                new.append((x1, b, yy))
        new.append((x, x1, y + bh + gap))
        sky = sorted(new)
    return out


def _mosaic(pos, W, limit, gap=5.0):
    """Grow each frame down, then right, into empty space (never into another frame) for a tidy grid."""
    rects = [[b[3], b[4], b[5], b[6]] for b in pos]

    def hits(i, x, y, w, h):
        for j, (X, Y, Wd, Hd) in enumerate(rects):
            if j != i and x < X + Wd + gap - 1e-6 and X < x + w + gap - 1e-6 and y < Y + Hd + gap - 1e-6 and Y < y + h + gap - 1e-6:
                return True
        return False
    for i in range(len(rects)):
        x, y, w, h = rects[i]
        lo, hi = h, max(h, limit(x, x + w) - y)
        while hi - lo > 0.5:              # binary search the tallest collision-free height
            mid = (lo + hi) / 2
            if hits(i, x, y, w, mid):
                hi = mid
            else:
                lo = mid
        rects[i][3] = lo
    for i in range(len(rects)):
        x, y, w, h = rects[i]
        lo, hi = w, max(w, W - x)
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            if hits(i, x, y, mid, h) or y + h > limit(x, x + mid) + 0.01:
                hi = mid
            else:
                lo = mid
        rects[i][2] = lo
    return [(b[0], b[1], b[2], r[0], r[1], r[2], r[3], b[5], b[6]) for b, r in zip(pos, rects)]


def render(circuit: Circuit, rev: str = "", date: str = "", company: str = "Morph", path_hint: str = "schematic.svg") -> tuple[str, dict]:
    """Render the circuit to a schematic sheet SVG. Returns (svg, layout json)."""
    blocks = list(circuit.blocks) or [("main", circuit.title.upper())]
    keys = [b[0] for b in blocks]
    for c in circuit.comps:
        if c.block not in keys:
            blocks.append((c.block or "misc", (c.block or "misc").upper()))
            keys.append(c.block or "misc")
    pad = 4.0
    title_h = 4.5
    fits = None
    for sheet_name, SW, SH in SHEETS:
      for frac in (0.30, 0.36, 0.44, 0.24, 0.52):
        inner = (SW - 2 * 10.0, SH - 2 * 10.0)
        content_w = inner[0] - 8.0
        laid = []
        for key, title in blocks:
            comps = [c for c in circuit.comps if (c.block or "misc") == key]
            if not comps:
                continue
            target = content_w * min(frac * (1.3 if key in ("mcu", "ctrl", "power") else 1.0), 0.95)
            placed, bw, bh = _block_layout(comps, target)
            laid.append((key, title, placed, bw + 2 * pad, bh + 2 * pad + title_h))
        tb_h, tb_w = 32.0, 180.0
        notes_h = 3.2 * (len(circuit.notes) + 2) if circuit.notes else 0
        notes_w = max([_tw(f"{i + 1}. {n}", 1.35) for i, n in enumerate(circuit.notes)] + [0]) + 4
        full = inner[1] - 8.0

        def limit(x0, x1, full=full, tb_h=tb_h, tb_w=tb_w, notes_h=notes_h, notes_w=notes_w, cw=content_w):
            lim = full
            if x1 > cw - tb_w + 2:
                lim = min(lim, full - tb_h - 5)
            if x0 < notes_w:
                lim = min(lim, full - notes_h - 3)
            return lim
        pos = _skyline(laid, content_w, limit=limit)
        total_h = max((b[4] + b[6] for b in pos), default=0.0)
        avail = full
        if all(b[7] <= 0 for b in pos) and all(b[5] <= content_w for b in pos):
            fill = total_h / avail
            if fits is None or fill > fits[6]:
                fits = (sheet_name, SW, SH, inner, content_w, pos, fill)
      if fits:
        break
    sheet_name, SW, SH, inner, content_w, pos = (fits or (sheet_name, SW, SH, inner, content_w, pos, 1.0))[:6]
    full = inner[1] - 8.0
    notes_h = 3.2 * (len(circuit.notes) + 2) if circuit.notes else 0
    notes_w = max([_tw(f"{i + 1}. {n}", 1.35) for i, n in enumerate(circuit.notes)] + [0]) + 4

    def limit2(x0, x1):
        lim = full
        if x1 > content_w - 180.0 + 2:
            lim = min(lim, full - 37.0)
        if x0 < notes_w:
            lim = min(lim, full - notes_h - 3)
        return max(lim, max(b[4] + b[6] for b in pos if b[3] < x1 and b[3] + b[5] > x0) if pos else lim)
    pos = _mosaic(pos, content_w, limit2)
    ox, oy = 10.0 + 4.0, 10.0 + 4.0
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SW} {SH}" width="{SW * 4:.0f}" height="{SH * 4:.0f}" '
           f'font-family="{FONT}">', f'<rect x="0" y="0" width="{SW}" height="{SH}" fill="{C["paper"]}"/>']
    svg.append(_frame(SW, SH))
    layout = {"sheet": sheet_name, "size_mm": [SW, SH], "blocks": [], "symbols": []}
    for key, title, placed, bx, by, bw2, rh, nat_w, nat_h in pos:
        X, Y = ox + bx, oy + by
        svg.append(f'<rect x="{X:.3f}" y="{Y:.3f}" width="{bw2:.3f}" height="{rh:.3f}" rx="1.2" fill="{C["blockfill"]}" '
                   f'fill-opacity="0.55" stroke="{C["block"]}" stroke-width="0.25" stroke-dasharray="2.0 1.0"/>')
        svg.append(f'<rect x="{X:.3f}" y="{Y:.3f}" width="{min(_tw(title, 1.6) * 1.14 + 4.5, bw2):.3f}" height="4.0" rx="1.2" fill="{C["block"]}"/>')
        svg.append(_t(X + 2.0, Y + 2.85, title, 1.6, "#ffffff", weight="700", extra=' letter-spacing="0.12"'))
        cx_off = max((bw2 - nat_w) / 2, 0.0)
        cy_off = max(min((rh - nat_h) / 2, 12.0), 0.0)
        for el, ex, ey in placed:
            gx, gy = X + pad + ex + cx_off, Y + pad + title_h + ey + cy_off
            svg.append(f'<g transform="translate({gx:.3f} {gy:.3f})" data-ref="{_e(el.ref)}">{el.svg}</g>')
            layout["symbols"].append({"ref": el.ref, "x": round(gx, 2), "y": round(gy, 2), "w": round(el.w, 2), "h": round(el.h, 2),
                                      "pins": {k: [round(gx + px, 2), round(gy + py, 2)] for k, (px, py) in el.pins.items()}})
        layout["blocks"].append({"key": key, "title": title, "x": round(X, 2), "y": round(Y, 2), "w": round(bw2, 2), "h": round(rh, 2)})
    # notes + title block
    tbw, tbh = 180.0, 32.0
    tbx, tby = SW - 10.0 - tbw, SH - 10.0 - tbh
    if circuit.notes:
        ny = SH - 10.0 - 3.2 * len(circuit.notes) - 4.0
        svg.append(_t(ox, ny, "NOTES", 1.6, C["note"], weight="700"))
        for i, n in enumerate(circuit.notes):
            svg.append(_t(ox, ny + 3.2 * (i + 1), f"{i + 1}. {n}", 1.35, C["note"]))
    svg.append(_title_block(tbx, tby, tbw, tbh, circuit, sheet_name, rev, date, company, path_hint))
    svg.append("</svg>")
    return "".join(svg), layout


def _frame(W, H) -> str:
    col = C["frame"]
    m = 10.0
    s = [f'<rect x="{m / 2}" y="{m / 2}" width="{W - m}" height="{H - m}" fill="none" stroke="{col}" stroke-width="0.25"/>',
         f'<rect x="{m}" y="{m}" width="{W - 2 * m}" height="{H - 2 * m}" fill="none" stroke="{col}" stroke-width="0.35"/>']
    ncol = max(4, int(round((W - 2 * m) / 50)))
    nrow = max(3, int(round((H - 2 * m) / 50)))
    for i in range(ncol):
        x0 = m + (W - 2 * m) * i / ncol
        x1 = m + (W - 2 * m) * (i + 1) / ncol
        if i:
            s.append(_l(x0, m / 2, x0, m, col, 0.2) + _l(x0, H - m, x0, H - m / 2, col, 0.2))
        s.append(_t((x0 + x1) / 2, m / 2 + 3.4, str(i + 1), 2.0, col, "middle"))
        s.append(_t((x0 + x1) / 2, H - m / 2 - 1.6, str(i + 1), 2.0, col, "middle"))
    for j in range(nrow):
        y0 = m + (H - 2 * m) * j / nrow
        y1 = m + (H - 2 * m) * (j + 1) / nrow
        if j:
            s.append(_l(m / 2, y0, m, y0, col, 0.2) + _l(W - m, y0, W - m / 2, y0, col, 0.2))
        s.append(_t(m / 2 + 2.5, (y0 + y1) / 2 + 0.7, chr(65 + j), 2.0, col, "middle"))
        s.append(_t(W - m / 2 - 2.5, (y0 + y1) / 2 + 0.7, chr(65 + j), 2.0, col, "middle"))
    return "".join(s)


def _title_block(x, y, w, h, circuit, sheet, rev, date, company, path_hint) -> str:
    col = C["frame"]
    s = [f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="#fbfaf5" stroke="{col}" stroke-width="0.35"/>']
    r1, r2, r3 = y + 7.0, y + 19.0, y + 25.5
    s += [_l(x, r1, x + w, r1, col, 0.2), _l(x, r2, x + w, r2, col, 0.2), _l(x, r3, x + w, r3, col, 0.2),
          _l(x + 110, r2, x + 110, y + h, col, 0.2), _l(x + 140, r2, x + 140, r3, col, 0.2)]
    s.append(_t(x + 2, y + 2.6, "Company", 1.1, col))
    s.append(_t(x + 2, y + 5.9, company, 2.2, "#1a1a1a", weight="700"))
    s.append(_t(x + w - 2, y + 5.9, "Generated by Morph · electronics2 + schematic.py", 1.3, col, "end"))
    s.append(_t(x + 2, r1 + 2.6, "Title", 1.1, col))
    s.append(_t(x + 2, r1 + 8.4, circuit.title, 3.2, "#1a1a1a", weight="700"))
    s.append(_t(x + 2, r1 + 11.2, circuit.subtitle, 1.4, "#3a3a3a"))
    s.append(_t(x + 2, r2 + 2.6, "File", 1.1, col))
    s.append(_t(x + 2, r2 + 5.4, path_hint, 1.5, "#1a1a1a"))
    s.append(_t(x + 112, r2 + 2.6, "Size", 1.1, col))
    s.append(_t(x + 112, r2 + 5.4, sheet, 1.6, "#1a1a1a", weight="700"))
    s.append(_t(x + 142, r2 + 2.6, "Rev", 1.1, col))
    s.append(_t(x + 142, r2 + 5.4, rev or "A", 1.6, "#1a1a1a", weight="700"))
    s.append(_t(x + 2, r3 + 2.6, "Date", 1.1, col))
    s.append(_t(x + 2, r3 + 5.4, date, 1.5, "#1a1a1a"))
    s.append(_t(x + 112, r3 + 2.6, "Sheet", 1.1, col))
    s.append(_t(x + 112, r3 + 5.4, "1 / 1", 1.5, "#1a1a1a"))
    n = sum(1 for c in circuit.comps if c.kind != "MH")
    s.append(_t(x + w - 2, r3 + 5.4, f"{n} parts · {len(circuit.nets())} nets", 1.4, col, "end"))
    return "".join(s)


def write(circuit: Circuit, outdir: str, rev: str = "", date: str = "", name: str = "schematic") -> dict:
    """Write <name>.svg and <name>.json (netlist + symbol positions)."""
    svg, layout = render(circuit, rev=rev, date=date, path_hint=f"{name}.svg")
    with open(f"{outdir}/{name}.svg", "w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n' + svg)
    data = circuit.to_json()
    data["layout"] = layout
    with open(f"{outdir}/{name}.json", "w") as fh:
        json.dump(data, fh, indent=1)
    return {"svg": f"{name}.svg", "json": f"{name}.json", "sheet": layout["sheet"], "nets": data["nets"]}
