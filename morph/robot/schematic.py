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
