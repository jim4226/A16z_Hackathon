"""Manufacturing planner + BOM for a Morph robot assembly.

    mfg = plan(design, asm, outdir, efinal)     # process per part group, DFM, machine files, cost/lead per build config
    bom = bom(design, asm, mfg, efinal)          # merged make/buy rows with vendors, totals per build config

What it does
------------
* Groups identical parts (`Part.group`) so each is made once with qty n.
* Picks a process per group for each build config (prototype / pilot / production) from the part's class
  (plate, sheet, cover, gear, finger, pad, turned, structural), honouring `Part.process_options` and
  per-part `meta["process_by_config"]` overrides; the generator's own `Part.process` wins for the active
  config whenever it suits that config.
* Costs every config every time (so the Studio can compare) with transparent, parametric models of real
  vendors (JLC3DP, Craftcloud, Xometry, Protolabs, JLCCNC, PCBWay, SendCutSend, Fictiv, JLCPCB). These are
  ESTIMATES from Morph's formulas, not live quotes; every part entry carries its `cost_model` (formula +
  inputs) so a reviewer can check the arithmetic.
* For the active config only: DFM checks and machine files under `outdir/<group>/`:
    printing        <g>.stl (oriented on the bed) + <g>.3mf (qty copies arranged, print settings in metadata)
    CNC / laser     <g>.dxf (R12: layers CUT / BEND / ENGRAVE) + <g>.nc (2.5D G-code: holes, contour, tabs)
                    + <g>_toolpath.svg / <g>.svg previews
    sheet metal     <g>_flat.dxf + <g>_bends.csv (angle, radius, K-factor, BA, BD) + <g>_flat.svg + <g>.stl
    molding         <g>.stl + <g>_molding.txt (draft, wall, gate, ejection, tool estimate) + <g>_molding.json
    PCBA / harness  gerbers + JLC BOM/CPL copied from the electronics stage, harness cut list + drawing
  plus README.txt (what to send where) and mfg_<config>.zip with everything.

Contract keys are all present; extra keys (backward-compatible) the Studio may use:
  plan:   quantity, cost_basis, readme, package (zip), configs[*].{parts_cost, purchased_cost, setup_cost,
          fab_lead_days, processes, curve [{qty, unit_cost}], breakeven_vs_pilot_qty (production)},
          parts[*].{class, quotes [{vendor,url,unit_price,lead_days,note,setup_cost,tooling_cost}], cost_model
          {formula, inputs}, tooling_cost, how, in_bom, parts, print | cnc | sheet}
  bom:    rows[*].{sku, source, group, process, material, files, spec, torque_nm, note}, totals.{tooling, lines,
          pieces, quantity, config}, cost_basis
Process choice: an assembly that declares meta['build_config'] (the arm generator does) keeps its own
Part.process in that config, except that a mold is swapped for a listed alternative when it cannot pay off
at the order quantity (noted on the part); other configs use the class defaults in PREFS. Assemblies without
build_config (stub, imported robots) use process_options as the alternatives; no options = fixed process.

Units: mm, g, USD. Lead times are calendar days including typical shipping to the US.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import zipfile
from collections import Counter, OrderedDict, defaultdict

import numpy as np
import shapely
import trimesh
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient

from . import catalog
from ..fab import GcodeWriter, SAFE_Z

CONFIGS = ("prototype", "pilot", "production")
FAB_PROCESSES = ("fdm_print", "sla_print", "mjf_print", "cnc_mill", "cnc_turn", "laser_cut", "sheet_metal",
                 "injection_mold")
PRINT_PROCESSES = ("fdm_print", "sla_print", "mjf_print")
NON_FAB_KINDS = ("fastener", "cable", "visual", "actuator", "horn", "bearing", "electronics", "sensor")
COST_BASIS = ("Estimates from Morph's parametric cost models (formula + inputs on every part); "
              "not live vendor quotes. Upload the files to the linked vendor for a binding price.")

PROC_LABEL = {"fdm_print": "FDM print", "sla_print": "SLA print", "mjf_print": "MJF PA12", "cnc_mill": "CNC milled",
              "cnc_turn": "CNC turned", "laser_cut": "laser cut", "sheet_metal": "sheet metal",
              "injection_mold": "injection molded", "pcb_assembly": "PCBA", "cable_assembly": "harness",
              "purchased": "purchased"}

# ---------------------------------------------------------------- vendors (real companies; prices are model estimates)
VENDORS = {
    "inhouse_fdm": ("In-house FDM", ""),
    "inhouse_sla": ("In-house SLA", ""),
    "inhouse_harness": ("In-house crimp", ""),
    "jlc3dp": ("JLC3DP", "https://jlc3dp.com/3d-printing-quote"),
    "craftcloud": ("Craftcloud", "https://craftcloud3d.com/"),
    "xometry_3dp": ("Xometry", "https://www.xometry.com/capabilities/3d-printing-service/"),
    "xometry_cnc": ("Xometry", "https://www.xometry.com/capabilities/cnc-machining-service/"),
    "xometry_sheet": ("Xometry", "https://www.xometry.com/capabilities/sheet-metal-fabrication/"),
    "xometry_im": ("Xometry", "https://www.xometry.com/capabilities/injection-molding-service/"),
    "protolabs_cnc": ("Protolabs", "https://www.protolabs.com/services/cnc-machining/"),
    "protolabs_sheet": ("Protolabs", "https://www.protolabs.com/services/sheet-metal-fabrication/"),
    "protolabs_im": ("Protolabs", "https://www.protolabs.com/services/injection-molding/"),
    "protolabs_3dp": ("Protolabs", "https://www.protolabs.com/services/3d-printing/"),
    "jlccnc": ("JLCCNC", "https://jlccnc.com/"),
    "pcbway_cnc": ("PCBWay", "https://www.pcbway.com/rapid-prototyping/"),
    "pcbway_pcba": ("PCBWay", "https://www.pcbway.com/orderonline.aspx"),
    "sendcutsend": ("SendCutSend", "https://sendcutsend.com/"),
    "fictiv": ("Fictiv", "https://www.fictiv.com/"),
    "jlcpcb": ("JLCPCB", "https://cart.jlcpcb.com/quote"),
}

# material data used by the cost models: density g/cc, stock price $/kg
MAT = {
    "PLA": (1.24, 20.0), "PETG": (1.27, 22.0), "ABS": (1.04, 20.0), "ASA": (1.07, 24.0), "PC": (1.20, 30.0),
    "Nylon-CF": (1.15, 60.0), "TPU": (1.21, 35.0), "Tough resin": (1.15, 150.0), "PA12": (1.01, 0.0),
    "Al6061": (2.70, 9.0), "Al7075": (2.81, 14.0), "Al5052": (2.68, 8.0), "Steel": (7.85, 3.0),
    "Stainless": (8.0, 9.0), "Delrin": (1.41, 18.0), "Acrylic": (1.18, 12.0), "PC/ABS": (1.15, 4.0),
    "POM": (1.41, 3.5), "PA66-GF30": (1.36, 4.5), "TPE 60A": (1.10, 6.0), "Brass": (8.5, 12.0),
}
FDM_MATS = ("PLA", "PETG", "ABS", "ASA", "Nylon-CF", "TPU")
FDM_BED = (250.0, 210.0, 220.0)        # Prusa MK4-class build volume
MJF_BED = (380.0, 284.0, 380.0)
SLA_BED = (200.0, 125.0, 210.0)
BEDS = {"fdm_print": FDM_BED, "sla_print": SLA_BED, "mjf_print": MJF_BED}
SHEET_GAUGES = (0.81, 1.02, 1.27, 1.6, 2.03, 2.29, 2.54, 3.18, 4.06, 4.83, 6.35)   # SendCutSend 5052 stock, mm
PLATE_STOCK = (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0)


def _r(x, n=2) -> float:
    return float(round(float(x), n))


def _vf(n_order: int, k: float = 0.1, floor: float = 0.6) -> float:
    """Per-piece volume factor: 10x the order quantity -> k cheaper (vendor batching), floored."""
    return max(floor, 1.0 - k * math.log10(max(1, n_order)))


def _quote(vkey, price, lead, note="", setup=0.0, tooling=0.0):
    name, url = VENDORS[vkey]
    return {"vendor": name, "url": url, "unit_price": _r(price), "lead_days": int(round(lead)),
            "note": note, "setup_cost": _r(setup), "tooling_cost": _r(tooling)}


def pretty(group: str) -> str:
    words = [w for w in re.split(r"[_\s]+", str(group)) if w]
    out = []
    for w in words:
        if not out or out[-1].lower() != w.lower():
            out.append(w)
    s = " ".join(out)
    return s[:1].upper() + s[1:]


# ================================================================== fasteners
_FKINDS = [("insert", ("insert", "heat-set", "heat_set", "heatset")), ("standoff", ("standoff",)),
           ("setscrew", ("set screw", "setscrew", "set_screw", "grub")), ("nut", ("nut", "nylock")),
           ("washer", ("washer",)), ("bhcs", ("bhcs", "button")), ("fhcs", ("fhcs", "flat", "countersunk", "csk")),
           ("tie", ("cable tie", "zip tie", "tie")), ("shcs", ("shcs", "socket", "cap", "screw", "bolt"))]
_FLABEL = {"shcs": "socket head cap screw", "bhcs": "button head screw", "fhcs": "flat head screw (countersunk)",
           "insert": "heat-set insert", "nut": "nylock nut", "washer": "washer", "standoff": "hex standoff (F-F)",
           "setscrew": "set screw", "tie": "cable tie"}
_FSHORT = {"shcs": "SHCS", "bhcs": "BHCS", "fhcs": "FHCS", "insert": "heat-set insert", "nut": "nylock nut",
           "washer": "washer", "standoff": "standoff", "setscrew": "set screw", "tie": "cable tie"}
_FPRICE = {"shcs": (0.10, 0.04, 100), "bhcs": (0.11, 0.05, 100), "fhcs": (0.11, 0.05, 100),
           "insert": (0.16, 0.06, 100), "nut": (0.05, 0.02, 100), "washer": (0.03, 0.01, 100),
           "standoff": (0.45, 0.15, 25), "setscrew": (0.12, 0.05, 50), "tie": (0.04, 0.02, 100)}   # mcmaster, amazon, pack


def parse_fastener(spec) -> dict:
    """'M3x8', 'M3×8 SHCS', 'M3x8_shcs', 'M3 heat-set insert', 'cable tie 100 mm' -> normalized fastener dict."""
    s = str(spec or "").strip()
    low = s.lower().replace("×", "x")
    m = re.search(r"m(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", low)
    size = length = None
    if m:
        size, length = f"M{m.group(1)}", float(m.group(2))
    else:
        m = re.search(r"\bm(\d+(?:\.\d+)?)\b", low) or re.search(r"(?:^|_)m(\d+(?:\.\d+)?)(?:_|$)", low)
        if m:
            size = f"M{m.group(1)}"
    kind = "shcs"
    for k, words in _FKINDS:
        if any(w in low for w in words):
            kind = k
            break
    if size is None and kind == "shcs" and "tie" not in low:
        kind = "other"
    if size and size not in catalog.FASTENER_SIZES:
        size = size  # keep exotic sizes; torque/tool lookups fall back
    if kind in ("nut", "washer", "insert"):
        length = None if kind != "insert" else length
    lt = f"×{length:g}" if length else ""
    if kind == "other":
        short = label = s
        key = s
    elif kind == "tie":
        mm = re.search(r"(\d+)\s*mm", low)
        short = f"cable tie {mm.group(1)} mm" if mm else "cable tie"
        label = short
        key = short
    else:
        short = f"{size}{lt} {_FSHORT[kind]}".strip()
        label = f"{size}{lt} {_FLABEL[kind]}".strip()
        key = short
    torque = None
    if size in catalog.FASTENER_TORQUE_NM and kind in ("shcs", "bhcs", "fhcs"):
        torque = catalog.FASTENER_TORQUE_NM[size]
    elif size in catalog.FASTENER_TORQUE_NM and kind == "setscrew":
        torque = round(catalog.FASTENER_TORQUE_NM[size] * 0.6, 2)
    drive = None
    if size in catalog.FASTENER_SIZES and kind in ("shcs", "bhcs", "fhcs", "setscrew"):
        key_mm = catalog.FASTENER_SIZES[size][2]
        if kind in ("bhcs", "fhcs"):
            key_mm = {"M2": 1.3, "M2.5": 1.5, "M3": 2.0, "M4": 2.5, "M5": 3.0}.get(size, key_mm)
        if kind == "setscrew":
            key_mm = {"M2": 0.9, "M2.5": 1.3, "M3": 1.5, "M4": 2.0, "M5": 2.5}.get(size, key_mm)
        drive = f"{key_mm:g} mm hex key"
    elif kind == "nut" and size:
        drive = f"{ {'M2': 4.0, 'M2.5': 5.0, 'M3': 5.5, 'M4': 7.0, 'M5': 8.0}.get(size, 5.5):g} mm nut driver"
    elif kind == "standoff" and size:
        drive = f"{ {'M2': 4.0, 'M2.5': 5.0, 'M3': 5.5}.get(size, 5.0):g} mm nut driver"
    elif kind == "insert":
        drive = f"soldering iron + {size or 'M3'} insert tip"
    return {"spec": short, "label": label, "key": key, "size": size, "length": length, "kind": kind,
            "torque_nm": torque, "drive": drive}


def fastener_vendors(f: dict) -> list:
    mc, amz, pack = _FPRICE.get(f["kind"], (0.10, 0.05, 100))
    q = f["label"]
    return [catalog.V("mcmaster", q, mc, note=f"sold in packs of {pack}; price per piece"),
            catalog.V("amazon", f"{f['spec']} assortment" if f["kind"] != "tie" else "cable ties 100mm", amz,
                      note="assortment kit; price per piece")]


def _instances(asm) -> "OrderedDict[str, dict]":
    """Purchased component instances: prefix -> {sku, kind, parts, step, link} (a servo = many sub-parts)."""
    out = OrderedDict()
    for p in asm.parts:
        if p.process != "purchased" or not p.sku or p.kind in ("fastener", "cable"):
            continue
        prefix = p.name
        g = p.group or ""
        if g.startswith(p.sku + "_"):
            suffix = g[len(p.sku) + 1:]
            if p.name.endswith("_" + suffix):
                prefix = p.name[: -len(suffix) - 1]
        e = out.setdefault(prefix, {"sku": p.sku, "kind": p.kind, "parts": [], "step": p.step, "link": p.link, "n": 1,
                                    "label": p.meta.get("label", "")})
        e["parts"].append(p.name)
        e["n"] = max(e["n"], int(p.meta.get("qty", 1) or 1))      # one mesh can stand for several pieces (4 feet)
        if p.kind in ("actuator", "electronics", "pcb", "bearing", "sensor"):
            e["kind"] = p.kind
    return out


def actuator_counts(asm) -> Counter:
    """Actuators per catalog key from the joints (a 'dual' joint carries meta['count'] = 2)."""
    out = Counter()
    for j in asm.joints:
        if j.actuator and not j.mimic:
            out[j.actuator] += int((j.meta or {}).get("count", 1) or 1)
    return out


def _short_name(name: str) -> str:
    return re.split(r" \(|, ", str(name))[0]


_GENERIC = {   # purchased bits generators use that are not catalog entries
    "rubber_foot": {"name": "Self-adhesive rubber foot (14 mm)", "vendors": [
        catalog.V("amazon", "self adhesive rubber feet 14mm", 0.12, note="pack of 50; price per piece"),
        catalog.V("mcmaster", "adhesive-back rubber bumper", 0.35)]},
    "cable_tie": {"name": "Cable tie 100 mm", "vendors": [catalog.V("amazon", "cable ties 100mm", 0.02)]},
}


def fabricated(p) -> bool:
    """A part Morph writes make files for (printed, cut, machined, molded). Decoration ("visual": the upper arm's logo
    inlay), fasteners and cables are not, so they have no print file, BOM line or place in the manual's parts tables."""
    return p.process in FAB_PROCESSES and p.kind not in ("fastener", "cable", "visual")


def _fab_parts(asm):
    return [p for p in asm.parts if fabricated(p)]


def step_fasteners(asm) -> dict:
    """Fasteners per assembly step: {step_index: [{spec,label,qty,torque_nm,drive,kind,source}]}.

    Sources, in order: Step.fasteners lists, fastener parts (kind == "fastener", spec from meta['spec'] / sku /
    group / name; meta['qty'] for merged screws). Per (step, spec) the larger count wins (they describe the same
    hardware). When the assembly carries no fastener data at all (early / stub generators) the hardware is
    inferred from what each step installs, and marked source="inferred"."""
    by_step = defaultdict(OrderedDict)
    have = False
    for s in asm.steps:
        for f in s.fasteners or []:
            d = parse_fastener(f.get("spec", ""))
            d["qty"] = int(f.get("qty", 1) or 1)
            if f.get("torque_nm") is not None:
                d["torque_nm"] = f["torque_nm"]
            d["source"] = "step"
            cur = by_step[s.index].get(d["key"])
            by_step[s.index][d["key"]] = d if cur is None else {**cur, "qty": cur["qty"] + d["qty"]}
            have = True
    counted = defaultdict(Counter)
    proto = {}
    for p in asm.parts:
        if p.kind != "fastener":
            continue
        # size/length from the first token that has them, kind from anywhere (meta spec "M4x10" + sku "M4x10_bhcs")
        spec = " ".join(str(x) for x in (p.meta.get("spec"), p.meta.get("kind"), p.sku, p.group or p.name) if x)
        d = parse_fastener(spec)
        counted[p.step][d["key"]] += int(p.meta.get("qty", p.meta.get("count", 1)) or 1)
        proto[d["key"]] = d
        have = True
    for st, cnt in counted.items():
        for key, n in cnt.items():
            cur = by_step[st].get(key)
            if cur is None or cur["qty"] < n:
                by_step[st][key] = {**proto[key], **(cur or {}), "qty": n, "source": "parts"}
    if not have:
        return _infer_fasteners(asm)
    return {k: list(v.values()) for k, v in by_step.items()}


def _infer_fasteners(asm) -> dict:
    inst = _instances(asm)
    out = defaultdict(OrderedDict)

    def add(step, spec, qty):
        d = parse_fastener(spec)
        cur = out[step].get(d["key"])
        out[step][d["key"]] = {**d, "qty": (cur["qty"] if cur else 0) + qty, "source": "inferred"}

    for prefix, e in inst.items():
        sku = e["sku"]
        if sku in catalog.ACTUATORS:
            fam = catalog.ACTUATORS[sku].get("family")
            if fam == "hobby_pwm":
                add(e["step"], "M3x10 SHCS", 4)
                add(e["step"], "M3 nylock nut", 4)
                add(e["step"], "M2.5x6 SHCS", 4)
            else:
                add(e["step"], "M2.5x8 SHCS", 4)        # case to bracket
                add(e["step"], "M2.5x6 SHCS", 4)        # horn to the next link
        elif sku in catalog.MODULES and e["kind"] in ("electronics", "pcb", "sensor"):
            role = catalog.MODULES[sku].get("role")
            if role in ("controller", "servo_driver", "power", "computer"):
                add(e["step"], "M2.5x6 SHCS", 4)
                add(e["step"], "M2.5x5 standoff", 4)
        elif sku in catalog.BEARINGS and "lazy" in sku:
            add(e["step"], "M3x8 SHCS", 4)                # turntable ring screws
    for p in _fab_parts(asm):
        n = p.name.lower()
        if p.kind == "pad":
            continue                                      # pads slide onto a dovetail
        if "finger" in n:
            add(p.step, "M3x16 SHCS", 2)
            add(p.step, "M3 nylock nut", 2)
        elif any(k in n for k in ("gear", "pinion")):
            add(p.step, "M2.5x6 SHCS", 4)
        elif p.meta.get("sheet"):
            add(p.step, "M3x6 BHCS", 4)
            add(p.step, "M3 nylock nut", 4)
        else:
            add(p.step, "M3 heat-set insert", 4)
            add(p.step, "M3x8 SHCS", 4)
    return {k: [f for f in v.values() if f["qty"] > 0] for k, v in out.items()}


# ================================================================== part classes, process choice
def part_class(p) -> str:
    n = p.name.lower()
    if p.meta.get("sheet"):
        return "sheet"
    if p.kind == "cover" or any(k in n for k in ("cover", "shell", "housing", "lid", "shroud", "enclosure", "bezel")):
        return "cover"
    if p.kind == "pad" or n.endswith("_pad"):
        return "pad"
    if any(k in n for k in ("gear", "pinion", "rack")):
        return "gear"
    if "finger" in n or "jaw" in n:
        return "finger"
    if p.process == "cnc_turn" or any(k in n for k in ("shaft", "axle", "spacer", "dowel")):
        return "turned"
    if p.profile is not None and p.thickness > 0:
        return "plate"
    e = np.sort(p.mesh.extents)
    if e[0] <= 0.16 * e[2] and e[0] <= 16.0:
        return "plate"
    return "structural"


PREFS = {
    "prototype": {"*": ["fdm_print", "sla_print", "laser_cut", "mjf_print", "sheet_metal", "cnc_mill", "cnc_turn",
                        "injection_mold"],
                  "sheet": ["fdm_print", "sheet_metal", "laser_cut"],
                  "turned": ["fdm_print", "cnc_turn", "cnc_mill"]},
    "pilot": {"sheet": ["sheet_metal", "laser_cut", "cnc_mill", "mjf_print", "fdm_print"],
              "plate": ["cnc_mill", "laser_cut", "sheet_metal", "mjf_print", "fdm_print"],
              "cover": ["mjf_print", "sla_print", "fdm_print", "cnc_mill"],
              "gear": ["mjf_print", "cnc_mill", "sla_print", "fdm_print"],
              "finger": ["sla_print", "mjf_print", "cnc_mill", "fdm_print"],
              "pad": ["sla_print", "mjf_print", "fdm_print"],
              "turned": ["cnc_turn", "cnc_mill", "mjf_print", "fdm_print"],
              "structural": ["cnc_mill", "mjf_print", "sla_print", "fdm_print"]},
    "production": {"sheet": ["sheet_metal", "laser_cut", "cnc_mill", "injection_mold"],
                   "plate": ["laser_cut", "cnc_mill", "sheet_metal", "injection_mold", "mjf_print"],
                   "cover": ["injection_mold", "mjf_print", "sla_print", "fdm_print"],
                   "gear": ["injection_mold", "cnc_mill", "mjf_print"],
                   "finger": ["injection_mold", "cnc_mill", "mjf_print", "sla_print"],
                   "pad": ["injection_mold", "sla_print", "mjf_print", "fdm_print"],
                   "turned": ["cnc_turn", "cnc_mill"],
                   "structural": ["injection_mold", "sheet_metal", "cnc_mill", "mjf_print", "fdm_print"]},
}


def choose_process(p, config: str, cls: str | None = None, active: bool = False, built_for: str = "") -> str:
    """Process for one part in one build config.
    * meta['process_by_config'][config] always wins.
    * The assembly was generated for this config (asm.meta['build_config']): the part's own process.
    * The assembly was generated for another config: that generator would build different geometry here, so use
      the config's class default, preferring one of the part's process_options when the config would use it.
    * No build_config on the assembly (stub / imported robots): process_options are the alternatives; a part
      with none keeps its process in every config."""
    cls = cls or part_class(p)
    over = (p.meta.get("process_by_config") or {}).get(config)
    if over:
        return over
    if built_for and config == built_for and p.process in FAB_PROCESSES:
        return p.process
    allowed = set(p.process_options or ()) | {p.process}
    allowed &= set(FAB_PROCESSES)
    prefs = PREFS[config].get(cls) or PREFS[config].get("*") or PREFS["prototype"]["*"]
    if built_for and config != built_for:
        top = prefs[:2]
        hit = next((x for x in top if x in allowed), None)
        return hit or prefs[0]
    if not p.process_options:                     # no alternatives offered: the part's process is fixed
        return p.process
    if config == "production" and cls == "plate":
        t = _plate_thickness(p)
        prefs = (["laser_cut", "sheet_metal", "cnc_mill"] if t <= 6.5 else ["cnc_mill", "laser_cut"]) + prefs
    if active and p.process in prefs and not (config != "prototype" and p.process == "fdm_print"):
        return p.process                          # the generator chose it for this build config
    for proc in prefs:
        if proc in allowed:
            return proc
    return p.process


def _plate_thickness(p) -> float:
    if p.profile is not None and p.thickness > 0:
        return float(p.thickness)
    if p.meta.get("sheet"):
        return float(p.meta["sheet"].get("thickness", 2.0))
    return float(np.min(p.mesh.extents))


def material_for(proc: str, cls: str, p) -> str:
    if proc == "fdm_print":
        return p.material if p.material in FDM_MATS else "PETG"
    if proc == "sla_print":
        return "Tough resin"
    if proc == "mjf_print":
        return "PA12"
    if proc in ("cnc_mill", "cnc_turn"):
        if p.material in ("Al6061", "Al7075", "Stainless", "Steel", "Delrin", "Brass"):
            return p.material
        return "Delrin" if cls in ("gear", "pad") else "Al6061"
    if proc == "sheet_metal":
        m = (p.meta.get("sheet") or {}).get("material") or p.material
        return m if m in ("Al5052", "Steel", "Stainless") else "Al5052"
    if proc == "laser_cut":
        return p.material if p.material in ("Acrylic", "Delrin", "Al5052", "Al6061", "Steel", "Stainless") else "Al5052"
    if proc == "injection_mold":
        return {"cover": "PC/ABS", "gear": "POM", "pad": "TPE 60A"}.get(cls, "PA66-GF30")
    return p.material


# ================================================================== geometry (per group, cached)
def _round_ring(ring, min_pts=8):
    """(is_round, centre, radius): a ring is a circle when its bounds are square and it fills that circle's area
    (vertex-distance tests fail on obround slots, whose vertices cluster on the end arcs)."""
    pts = np.asarray(ring.coords)
    poly = Polygon(ring)
    b = poly.bounds
    w, h = b[2] - b[0], b[3] - b[1]
    if len(pts) - 1 < min_pts or min(w, h) <= 0:
        return False, (poly.centroid.x, poly.centroid.y), math.sqrt(max(poly.area, 0) / math.pi)
    r = max(w, h) / 2
    ok = min(w, h) / max(w, h) > 0.97 and poly.area / (math.pi * r * r) > 0.95
    return ok, ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), math.sqrt(poly.area / math.pi)


def _rotz_to(axis) -> np.ndarray:
    """Rotation taking unit `axis` to +z."""
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(a, z)
    c = float(np.dot(a, z))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(4) if c > 0 else trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])
    return trimesh.transformations.rotation_matrix(math.acos(max(-1.0, min(1.0, c))), v)


def _ray_first_hit(origins, dirs, tris):
    """First hit along each ray (Möller-Trumbore): distance (inf if none) and triangle index. Each ray is only
    tested against triangles whose bounding sphere touches the ray line, so cost ~ rays x (few hundred)."""
    n = len(origins)
    best = np.full(n, np.inf)
    idx = np.full(n, -1)
    if n == 0 or len(tris) == 0:
        return best, idx
    v0 = tris[:, 0]
    e1, e2 = tris[:, 1] - v0, tris[:, 2] - v0
    C = tris.mean(axis=1)
    R2 = np.max(np.sum((tris - C[:, None, :]) ** 2, axis=2), axis=1) * 1.0001 + 1e-9
    CC = np.einsum("ij,ij->i", C, C)
    for i in range(n):
        o, d = origins[i], dirs[i]
        tp = C @ d - o @ d
        perp2 = CC - 2.0 * (C @ o) + o @ o - tp * tp
        cand = np.nonzero((perp2 <= R2) & (tp * np.abs(tp) >= -R2))[0]
        if len(cand) == 0:
            continue
        E1, E2 = e1[cand], e2[cand]
        p = np.cross(d, E2)
        det = np.einsum("ij,ij->i", p, E1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o - v0[cand]
        u = np.einsum("ij,ij->i", tv, p) * inv
        q = np.cross(tv, E1)
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", q, E2) * inv
        hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-6)
        if hit.any():
            t = np.where(hit, t, np.inf)
            j = int(np.argmin(t))
            best[i] = t[j]
            idx[i] = cand[j]
    return best, idx


class Geo:
    """Lazily computed manufacturing geometry of one part group (representative part)."""

    def __init__(self, part):
        self.p = part
        self.mesh = part.mesh
        self._c = {}

    def _memo(self, key, fn):
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    @property
    def volume_mm3(self):
        return self._memo("vol", lambda: float(self.p.volume_mm3))

    @property
    def volume_cm3(self):
        return self.volume_mm3 / 1000.0

    @property
    def area_cm2(self):
        return float(self.mesh.area) / 100.0

    @property
    def extents(self):
        return np.asarray(self.mesh.extents, float)

    @property
    def bbox_cm3(self):
        return float(np.prod(self.extents)) / 1000.0

    # ---------------------------------------------------- 2.5D profile
    @property
    def profile(self):
        return self._memo("profile", self._profile)[0]

    @property
    def thickness(self):
        return self._memo("profile", self._profile)[1]

    def _profile(self):
        p = self.p
        sheet = p.meta.get("sheet")
        if p.profile is not None and (p.thickness > 0 or sheet):
            poly = p.profile if p.profile.geom_type == "Polygon" else max(p.profile.geoms, key=lambda g: g.area)
            t = float(sheet.get("thickness", p.thickness)) if sheet else float(p.thickness)
            return orient(poly.buffer(0), 1.0), t
        axis = int(np.argmin(self.extents))
        a = np.zeros(3)
        a[axis] = 1.0
        R = _rotz_to(a)
        tri = self.mesh.triangles @ R[:3, :3].T
        polys = [Polygon(t[:, :2]) for t in tri]
        polys = [q for q in polys if q.area > 1e-6]
        sil = shapely.union_all(polys) if polys else Polygon()
        sil = sil.buffer(0.01).buffer(-0.01)
        if sil.geom_type != "Polygon":
            sil = max(sil.geoms, key=lambda g: g.area)
        sil = sil.simplify(0.02)
        b = sil.bounds
        sil = affinity.translate(sil, -b[0], -b[1])
        return orient(sil, 1.0), float(self.extents[axis])

    @property
    def holes(self):
        """Interior rings -> [{'c': (x, y), 'd': diameter, 'round': bool, 'ring': coords}]."""
        def calc():
            out = []
            for ring in self.profile.interiors:
                rnd, c, r = _round_ring(ring)
                out.append({"c": c, "d": 2 * r, "round": bool(rnd), "ring": np.asarray(ring.coords)[:-1]})
            return out
        return self._memo("holes", calc)

    @property
    def outer_round(self):
        def calc():
            rnd, c, r = _round_ring(self.profile.exterior, 16)
            return (c[0], c[1], r) if rnd else None
        return self._memo("outer_round", calc)

    @property
    def cut_len_mm(self):
        pr = self.profile
        return float(pr.exterior.length + sum(r.length for r in pr.interiors))

    @property
    def pierces(self):
        return 1 + len(self.profile.interiors)

    # ---------------------------------------------------- printing pose + overhang analysis
    @property
    def print_pose(self):
        return self.pose_for("fdm_print")

    def pose_for(self, proc: str):
        """The print orientation for a process: the best-scoring pose that fits its build volume."""
        return self._memo(("pose", proc), lambda: self._print_pose(BEDS.get(proc, FDM_BED)))

    def _print_pose(self, bed=FDM_BED):
        m = self.mesh
        n = m.face_normals
        a = m.area_faces
        ctr = m.triangles_center
        cands = [("as modelled", np.eye(4)), ("flip", trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])),
                 ("+x down", trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])),
                 ("-x down", trimesh.transformations.rotation_matrix(-math.pi / 2, [0, 1, 0])),
                 ("+y down", trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])),
                 ("-y down", trimesh.transformations.rotation_matrix(math.pi / 2, [1, 0, 0]))]
        flat = None
        if self.p.profile is not None and self.p.profile_frame is not None and self.p.thickness > 0:
            try:
                flat = np.linalg.inv(np.asarray(self.p.profile_frame, float))
                cands.insert(0, ("profile flat on the bed", flat))
            except np.linalg.LinAlgError:
                flat = None
        bx, by = sorted(bed[:2], reverse=True)
        poses = []
        for label, R in cands:
            Rm = R[:3, :3]
            nz = n @ Rm[2]
            z = ctr @ Rm[2]
            vz = m.vertices @ Rm[2]
            z0 = vz.min()
            over = (nz < -0.7071) & (z > z0 + 0.3)
            sup = float(a[over].sum())
            h = float(vz.max() - z0)
            contact = float(a[(nz < -0.999) & (z < z0 + 0.05)].sum())
            score = sup + 1.5 * h - 0.05 * contact - (25.0 if label.startswith("profile") else 0.0)
            # mean height of overhang above the bed ~ support column height
            sup_h = float(np.average(z[over] - z0, weights=a[over])) if over.any() else 0.0
            fx, fy = sorted((float(np.ptp(m.vertices @ Rm[0])), float(np.ptp(m.vertices @ Rm[1]))), reverse=True)
            poses.append({"label": label, "R": R, "support_mm2": sup, "support_h": sup_h, "height": h,
                          "contact_mm2": contact, "score": score, "fits": h <= bed[2] and fx <= bx and fy <= by})
        # only poses inside the build volume (the footprint may turn 90° on the bed); none fits: the part needs splitting
        fit = [c for c in poses if c["fits"]] or poses
        best = min(fit, key=lambda c: c["score"])
        prof = next((c for c in fit if c["label"].startswith("profile")), None)
        if prof is not None and prof["support_mm2"] <= max(25.0, best["support_mm2"] + 25.0):
            best = prof            # 2.5D parts print flat (profile on the bed) unless that needs real supports
        T = best["R"].copy()
        pm = m.copy()
        pm.apply_transform(T)
        ext = pm.extents
        if ext[0] > bed[0] or ext[1] > bed[1]:   # it fits turned 90° on the bed: turn it
            T = trimesh.transformations.rotation_matrix(math.pi / 2, [0, 0, 1]) @ T
            pm = m.copy()
            pm.apply_transform(T)
        b = pm.bounds
        shift = trimesh.transformations.translation_matrix([-(b[0][0] + b[1][0]) / 2, -(b[0][1] + b[1][1]) / 2, -b[0][2]])
        best["T"] = shift @ T
        pm.apply_transform(shift)
        best["mesh"] = pm
        best["overhang_pct"] = 100.0 * best["support_mm2"] / max(float(m.area), 1e-9)
        return best

    # ---------------------------------------------------- wall thickness (ray cast along the inward normal)
    @property
    def walls(self):
        return self._memo("walls", self._walls)

    def _walls(self, n_rays=140):
        m = self.mesh
        if len(m.faces) > 60000 or len(m.faces) == 0:
            return None
        rng = np.random.default_rng(7)
        prob = m.area_faces / m.area_faces.sum()
        n_rays = int(min(n_rays, max(24, 1.5e6 // len(m.faces))))      # ~1.5M ray-triangle tests per part
        fi = rng.choice(len(m.faces), size=n_rays, replace=True, p=prob)        # area-weighted statistics
        bc = rng.dirichlet((2.0, 2.0, 2.0), size=n_rays)                        # random point inside each face
        n = m.face_normals[fi]
        o = np.einsum("ij,ijk->ik", bc, m.triangles[fi]) - n * 1e-3
        t, j = _ray_first_hit(o, -n, m.triangles)
        ok = np.isfinite(t) & (j >= 0)
        if not ok.any():
            return None
        opp = np.einsum("ij,ij->i", -n[ok], m.face_normals[j[ok]]) > 0.5     # hit the opposite wall
        th = t[ok][opp]
        if len(th) < 5:
            return None
        return {"min": float(np.percentile(th, 3)), "p10": float(np.percentile(th, 10)),
                "median": float(np.median(th)), "max": float(np.percentile(th, 90))}

    # ---------------------------------------------------- molding: pull direction, draft, undercuts
    @property
    def mold(self):
        return self._memo("mold", self._mold)

    def _mold(self, n_rays=120):
        m = self.mesh
        n = m.face_normals
        a = m.area_faces
        rng = np.random.default_rng(11)
        n_rays = int(min(n_rays, max(16, 6e5 // max(len(m.faces), 1))))
        fi = rng.choice(len(m.faces), size=min(n_rays, len(m.faces)), replace=False, p=a / a.sum())
        best = None
        for axis in range(3):
            s = np.zeros(3)
            s[axis] = 1.0
            ns = n[fi] @ s
            o = m.triangles_center[fi]
            up_ok = ns > -0.02
            dn_ok = ns < 0.02
            if len(m.faces) <= 120000:
                tu, _ = _ray_first_hit(o + n[fi] * 0.02, np.tile(s, (len(fi), 1)), m.triangles)
                td, _ = _ray_first_hit(o + n[fi] * 0.02, np.tile(-s, (len(fi), 1)), m.triangles)
                up_ok &= ~np.isfinite(tu)
                dn_ok &= ~np.isfinite(td)
            under = ~(up_ok | dn_ok)
            frac = float(a[fi][under].sum() / max(a[fi].sum(), 1e-9))
            proj = float(np.abs(n @ s) @ a) / 2.0
            key = (round(frac, 3), -proj)
            if best is None or key < best["key"]:
                best = {"key": key, "axis": "xyz"[axis], "undercut_frac": frac, "proj_mm2": proj, "s": s}
        s = best["s"]
        ns = np.abs(n @ s)
        walls = ns < 0.5
        no_draft = walls & (ns < math.sin(math.radians(0.5)))
        low_draft = walls & (ns < math.sin(math.radians(1.0)))
        wall_area = float(a[walls].sum())
        h = float(np.ptp(m.vertices @ s))
        best.update({"wall_mm2": wall_area, "no_draft_mm2": float(a[no_draft].sum()),
                     "low_draft_mm2": float(a[low_draft].sum()), "depth_mm": h,
                     "side_actions": int(min(4, math.ceil(best["undercut_frac"] * m.area / 600.0)))
                     if best["undercut_frac"] > 0.01 else 0})
        best.pop("key")
        best["s"] = s.tolist()
        return best


# ================================================================== cost models (transparent formulas)
def _fdm_numbers(g: Geo, mat: str):
    pose = g.print_pose
    V = g.volume_cm3
    shell = min(V, g.area_cm2 * 0.16)                # 4 perimeters x 0.4 mm ~ 1.6 mm skin
    infill = max(V - shell, 0.0) * 0.25
    support = pose["support_mm2"] / 100.0 * min(max(pose["support_h"], 2.0) / 10.0, 4.0) * 0.15
    used = shell + infill + support
    grams = used * MAT.get(mat, (1.25, 22))[0]
    layers = pose["height"] / 0.2
    hours = (used * 1000.0 / 8.0 * 1.35 + layers * 2.0 + 240.0) / 3600.0
    return used, grams, hours


def quotes_for(proc: str, g: Geo, mat: str, n_order: int, cfg: str):
    """[(quote, cost_model)] for one part at an order quantity; cost_model = {formula, inputs}."""
    rho, per_kg = MAT.get(mat, (1.2, 10.0))
    V, A, bb = g.volume_cm3, g.area_cm2, g.bbox_cm3
    vf = _vf(n_order)
    if proc == "fdm_print":
        used, grams, hours = _fdm_numbers(g, mat)
        inhouse = grams / 1000.0 * per_kg + hours * 2.0 + 0.40
        jlc = max(1.50, 0.60 + 0.10 * used + 0.45 * hours) * vf
        cc = max(5.00, 2.50 + 0.16 * used + 0.90 * hours) * vf
        model = {"formula": "in-house = g x $/kg + h x $2/h + $0.40; JLC3DP = max(1.5, 0.6 + 0.10/cm3 + 0.45/h) x vf; "
                            "Craftcloud = max(5, 2.5 + 0.16/cm3 + 0.90/h) x vf",
                 "inputs": {"material_cm3": _r(used, 1), "grams": _r(grams, 1), "print_h": _r(hours, 2),
                            "usd_per_kg": per_kg, "vf": _r(vf, 3)}}
        q = [_quote("jlc3dp", jlc, 7, "FDM, ships from Shenzhen (DHL)"),
             _quote("craftcloud", cc, 8, "marketplace: picks a local print shop")]
        if cfg == "prototype":
            q.insert(0, _quote("inhouse_fdm", inhouse, 1, f"{hours:.1f} h on a 0.4 mm nozzle printer, {grams:.0f} g {mat}"))
        return q, model
    if proc == "sla_print":
        used = V * 1.10
        inhouse = used * 0.175 + 1.00
        jlc = max(1.00, 0.35 + 0.14 * used + 0.004 * bb) * vf
        cc = max(6.00, 2.00 + 0.38 * used) * vf
        model = {"formula": "JLC3DP = max(1, 0.35 + 0.14/cm3 + 0.004/bbox-cm3) x vf; Craftcloud = max(6, 2 + 0.38/cm3) x vf; "
                            "in-house = resin $175/L + $1",
                 "inputs": {"resin_cm3": _r(used, 1), "bbox_cm3": _r(bb, 1), "vf": _r(vf, 3)}}
        q = [_quote("jlc3dp", jlc, 7, "SLA tough resin, ships from Shenzhen"),
             _quote("craftcloud", cc, 8, "SLA tough resin, local partner")]
        if cfg == "prototype":
            q.insert(0, _quote("inhouse_sla", inhouse, 1, "Formlabs-class printer, tough resin"))
        return q, model
    if proc == "mjf_print":
        jlc = max(2.50, 0.80 + 0.28 * V + 0.012 * bb) * vf
        xo = max(28.0, 9.0 + 0.85 * V + 0.04 * bb) * vf
        pl = max(45.0, 15.0 + 1.10 * V + 0.05 * bb) * vf
        model = {"formula": "JLC3DP = max(2.5, 0.8 + 0.28/cm3 + 0.012/bbox-cm3) x vf; Xometry = max(28, 9 + 0.85/cm3 + 0.04/bbox-cm3) x vf; "
                            "Protolabs = max(45, 15 + 1.10/cm3 + 0.05/bbox-cm3) x vf",
                 "inputs": {"part_cm3": _r(V, 2), "bbox_cm3": _r(bb, 1), "vf": _r(vf, 3)}}
        return [_quote("jlc3dp", jlc, 9, "HP MJF PA12, bead-blasted grey/black dyed"),
                _quote("xometry_3dp", xo, 7, "HP MJF PA12, US network"),
                _quote("protolabs_3dp", pl, 5, "MJF PA12, expedited")], model
    if proc in ("cnc_mill", "cnc_turn"):
        flat = part_class(g.p) == "plate" or proc == "cnc_turn"
        ext = g.extents + 6.0
        stock_cm3 = float(np.prod(ext)) / 1000.0
        if proc == "cnc_turn":
            d = float(np.sort(g.extents)[1]) + 3.0
            stock_cm3 = math.pi * d * d / 4 * (float(g.extents.max()) + 6) / 1000.0
        removed = max(stock_cm3 - V, 0.0)
        mrr = 6.0 if mat.startswith("Al") or mat == "Brass" else (12.0 if mat in ("Delrin", "POM") else 2.5)
        holes = len(g.holes) if flat else 0
        minutes = removed / mrr + A / 18.0 + holes * 0.4 + 3.0
        setups = 1 if flat else 2
        material = stock_cm3 * rho / 1000.0 * per_kg * 1.4
        vfo = _vf(n_order, 0.08, 0.7)
        model = {"formula": "piece = (stock_kg x $/kg x 1.4 + min x shop $/h + $3 deburr) x margin x vf + (setup/op x ops + CAM) "
                            "/ order; shop rates $/h, $/op, CAM, margin: Xometry 80/75/35/1.30, Protolabs 110/120/60/1.35, "
                            "JLCCNC 25/8/0/1.15, PCBWay 30/12/0/1.15; minutes = removed/MRR + area/18 cm2/min + 0.4/hole + 3",
                 "inputs": {"stock_cm3": _r(stock_cm3, 1), "removed_cm3": _r(removed, 1), "mrr_cm3_min": mrr,
                            "machine_min": _r(minutes, 1), "setups": setups, "holes": holes, "order_qty": n_order}}
        q = []
        for vk, rate, op, cam, margin, lead, note in (
                ("xometry_cnc", 80.0, 75.0, 35.0, 1.30, 9, "3-axis, ISO 2768-m, as-machined"),
                ("protolabs_cnc", 110.0, 120.0, 60.0, 1.35, 4, "fast-turn, quote-to-part in days"),
                ("jlccnc", 25.0, 8.0, 0.0, 1.15, 11, "Shenzhen, ships DHL"),
                ("pcbway_cnc", 30.0, 12.0, 0.0, 1.15, 12, "Shenzhen, ships DHL")):
            setup = op * setups + cam
            run = (material + minutes * rate / 60.0 + 3.0) * margin * vfo
            q.append(_quote(vk, run + setup / n_order, lead, note, setup=setup))
        return q, model
    if proc in ("sheet_metal", "laser_cut"):
        t = g.thickness
        pr = g.profile
        b = pr.bounds
        sheet_cm2 = (b[2] - b[0] + 6) * (b[3] - b[1] + 6) / 100.0
        bends = len((g.p.meta.get("sheet") or {}).get("bends", [])) if proc == "sheet_metal" else 0
        cut_m = g.cut_len_mm / 1000.0 * max(1.0, t / 3.0)
        scs = (2.00 + cut_m * 1.40 + g.pierces * 0.06 + sheet_cm2 * t * 0.0055 * per_kg / 8.0 + bends * 1.60) * _vf(n_order, 0.12, 0.55)
        setup = 6.0 * bends
        model = {"formula": "SendCutSend = ($2 + cut_m x $1.40 + pierces x $0.06 + sheet_cm2 x t x $0.0055 + bends x $1.60) x vf; "
                            "Protolabs x2.8, Xometry x2.0, JLCCNC x0.55",
                 "inputs": {"thickness_mm": _r(t, 2), "cut_m": _r(cut_m, 3), "pierces": g.pierces, "sheet_cm2": _r(sheet_cm2, 1),
                            "bends": bends, "order_qty": n_order}}
        q = [_quote("sendcutsend", scs + setup / n_order, 5 if not bends else 7,
                    "fiber laser" + (" + press-brake bending" if bends else "") + ", free US shipping over $29", setup=setup)]
        if proc == "sheet_metal":
            q += [_quote("protolabs_sheet", scs * 2.8 + 60.0 / n_order, 8, "sheet metal, fast-turn", setup=60.0),
                  _quote("xometry_sheet", scs * 2.0 + 40.0 / n_order, 10, "sheet metal, US network", setup=40.0),
                  _quote("jlccnc", scs * 0.55 + 15.0 / n_order, 13, "sheet metal, ships DHL", setup=15.0)]
        else:
            q += [_quote("xometry_sheet", scs * 2.0, 9, "laser cutting"), _quote("jlccnc", scs * 0.55, 12, "laser cutting, ships DHL")]
        return q, model
    if proc == "injection_mold":
        mo = g.mold
        wall = (g.walls or {}).get("median", 2.5)
        wall_c = min(max(wall, 1.2), 6.0)
        proj = mo["proj_mm2"] / 100.0
        tool = 2800.0 + 22.0 * proj + 4.5 * A + 1400.0 * mo["side_actions"] + (900.0 if mo["depth_mm"] > 50 else 0.0)
        shot_g = V * rho * 1.08
        cycle = 12.0 + 2.5 * wall_c ** 2
        piece = shot_g / 1000.0 * per_kg + cycle * 55.0 / 3600.0 + 0.15
        press = 250.0
        model = {"formula": "tool = $2800 + $22/cm2 projected + $4.5/cm2 surface + $1400/side action (+$900 if deep); "
                            "piece = shot_g x $/kg + cycle_s x $55/h + $0.15; cycle = 12 + 2.5 x wall^2 s; press setup $250/order",
                 "inputs": {"projected_cm2": _r(proj, 1), "surface_cm2": _r(A, 1), "side_actions": mo["side_actions"],
                            "shot_g": _r(shot_g, 1), "wall_mm": _r(wall_c, 2), "cycle_s": _r(cycle, 1), "order_qty": n_order}}
        return [_quote("protolabs_im", piece * 1.6 + press / n_order, 22, "aluminium tool, T1 samples ~15 business days",
                       setup=press, tooling=tool),
                _quote("xometry_im", piece * 1.3 + press / n_order, 30, "P20 steel tool, 50k+ shot life",
                       setup=press, tooling=tool * 1.15),
                _quote("fictiv", piece * 1.0 + press / n_order, 38, "offshore steel tool, production volumes",
                       setup=press, tooling=tool * 0.7)], model
    raise ValueError(f"no cost model for {proc}")


def select_quote(quotes: list, cfg: str, n_order: int = 1) -> dict:
    """Prototype + production: lowest landed cost per piece (tooling spread over the order);
    pilot: time-weighted (3 %/day), because pilot builds are schedule-driven."""
    def landed(q):
        base = q["unit_price"] + q.get("tooling_cost", 0.0) / max(n_order, 1)
        return base * (1.0 + 0.03 * q["lead_days"]) if cfg == "pilot" else base
    return min(quotes, key=landed)


# ================================================================== groups
class Group:
    def __init__(self, key, parts):
        self.key = key
        self.parts = parts
        self.rep = parts[0]
        self.qty = len(parts)
        self.cls = part_class(self.rep)
        self.name = self.rep.meta.get("title") or pretty(key)
        self.step = min(p.step for p in parts)
        self.geo = Geo(self.rep)


def fab_groups(asm) -> list:
    groups = OrderedDict()
    for p in _fab_parts(asm):
        groups.setdefault(p.group or p.name, []).append(p)
    return [Group(k, v) for k, v in groups.items()]


def _landed(sel, n_order):
    return sel["unit_price"] + sel.get("tooling_cost", 0.0) / max(n_order, 1)


def _estimate(g: Group, cfg: str, nq: int, active_cfg: str, built_for: str = "") -> dict:
    proc = choose_process(g.rep, cfg, g.cls, active=cfg == active_cfg, built_for=built_for)
    mat = material_for(proc, g.cls, g.rep)
    n_order = g.qty * nq
    quotes, model = quotes_for(proc, g.geo, mat, n_order, cfg)
    sel = select_quote(quotes, cfg, n_order)
    note = ""
    if proc == "injection_mold" and cfg != "production":
        # a mold rarely pays off outside production volumes: take a listed alternative when it is clearly cheaper
        best = None
        for alt in [x for x in (g.rep.process_options or ()) if x in FAB_PROCESSES and x != proc]:
            amat = material_for(alt, g.cls, g.rep)
            aq, am = quotes_for(alt, g.geo, amat, n_order, cfg)
            asel = select_quote(aq, cfg, n_order)
            if best is None or _landed(asel, n_order) < _landed(best[3], n_order):
                best = (alt, amat, (aq, am), asel)
        if best and _landed(best[3], n_order) < 0.7 * _landed(sel, n_order):
            note = (f"generator suggested injection molding; at {n_order} pcs {PROC_LABEL[best[0]]} is cheaper "
                    f"(${_landed(best[3], n_order):.2f} vs ${_landed(sel, n_order):.2f} incl. tooling)")
            proc, mat, (quotes, model), sel = best
    return {"group": g.key, "qty": g.qty, "name": g.name, "process": proc, "material": mat, "class": g.cls,
            "vendor": sel["vendor"], "vendor_url": sel["url"], "unit_cost": _r(sel["unit_price"]),
            "setup_cost": _r(sel.get("setup_cost", 0.0)), "tooling_cost": _r(sel.get("tooling_cost", 0.0)),
            "lead_days": sel["lead_days"], "quotes": quotes, "cost_model": model, "files": [], "dfm": [], "notes": note,
            "step": g.step, "parts": [p.name for p in g.parts], "in_bom": True}


# ================================================================== electronics-derived groups (PCBA, harness)
def _elec_dir(outdir, efinal):
    if efinal and efinal.get("dir") and os.path.isdir(efinal["dir"]):
        return efinal["dir"]
    return os.path.join(os.path.dirname(os.path.abspath(outdir)), "electronics")


def _harness_cost(harness) -> tuple:
    mat = 0.0
    crimps = 0
    for w in harness:
        c = int(w.get("conductors", 2) or 2)
        L = float(w.get("length_mm", 150) or 150)
        mat += L * c * 0.00035 + 2 * c * 0.04 + 2 * 0.12
        crimps += 2 * c
    return mat, crimps


def _elec_estimates(design, asm, efinal, nq) -> list:
    if not efinal:
        pcb_parts = [p for p in asm.parts if p.process == "pcb_assembly"]
        if not pcb_parts:
            return []
        w, h = (pcb_parts[0].meta.get("size_mm") or [60.0, 40.0])[:2]
        board = {"size_mm": [w, h]}
        efinal = {"board": board}
    out = []
    src = efinal.get("sourcing") or []
    cats = {r.get("category") for r in src}
    board = efinal.get("board")
    if board:
        w, h = (board.get("size_mm") or [60.0, 40.0])[:2]
        cfgs = efinal.get("configs") or {}
        pcb_row = next((r for r in src if r.get("category") == "pcb"), None)
        if pcb_row:
            unit = float((pcb_row.get("selected") or {}).get("unit_price") or pcb_row.get("ext_price") or 0.0)
        elif (cfgs.get("custom_pcb") or {}).get("cost"):
            unit = float(cfgs["custom_pcb"]["cost"])
        else:
            unit = 2.0 + 0.02 * w * h / 100.0 * 10 + 15.0
        setup = 8.0 + 7.86                                  # JLCPCB assembly setup + stencil
        n_order = max(5, nq)
        unit_n = unit * _vf(n_order, 0.12, 0.55) + setup / n_order
        q = [_quote("jlcpcb", unit_n, 9, "2-layer PCBA, economic assembly, min order 5 boards", setup=setup),
             _quote("pcbway_pcba", unit_n * 1.35, 12, "turnkey PCBA", setup=setup * 2)]
        e = {"group": "controller_pcba", "qty": 1, "name": f"Controller PCBA ({w:.0f}×{h:.0f} mm)",
             "process": "pcb_assembly", "material": "FR4", "class": "pcb", "vendor": "JLCPCB",
             "vendor_url": VENDORS["jlcpcb"][1], "unit_cost": _r(unit_n), "setup_cost": _r(setup), "tooling_cost": 0.0,
             "lead_days": 9, "quotes": q,
             "cost_model": {"formula": "per-board landed cost from the electronics stage x vf + (assembly setup $8 + stencil $7.86) / order",
                            "inputs": {"board_cost": _r(unit), "order_qty": n_order}},
             "files": [], "dfm": [], "notes": "", "step": 1, "parts": [p.name for p in asm.parts if p.kind == "pcb"],
             "in_bom": pcb_row is None}
        drc = board.get("drc") or {}
        if drc and drc.get("pass") is False:
            e["dfm"].append("board DRC failed: fix before ordering")
        out.append(e)
    harness = efinal.get("harness") or []
    if harness:
        mat, crimps = _harness_cost(harness)
        in_house = mat
        vend = (mat * 1.6 + 1.2 * len(harness)) * _vf(nq, 0.12, 0.5)
        q = [_quote("inhouse_harness", in_house, 1, f"{crimps} crimps, ~{len(harness) * 4} min"),
             _quote("pcbway_cnc", vend, 12, "custom cable assembly")]
        sel = q[0] if nq < 20 else q[1]
        out.append({"group": "harness", "qty": 1, "name": f"Wiring harness ({len(harness)} wires)",
                    "process": "cable_assembly", "material": "Copper", "class": "harness", "vendor": sel["vendor"],
                    "vendor_url": sel["url"], "unit_cost": sel["unit_price"], "setup_cost": 0.0, "tooling_cost": 0.0,
                    "lead_days": sel["lead_days"], "quotes": q,
                    "cost_model": {"formula": "wire $0.35/m per conductor + $0.04/crimp + $0.12/housing; vendor = 1.6x + $1.2/wire",
                                   "inputs": {"wires": len(harness), "crimps": crimps}},
                    "files": [], "dfm": [], "notes": "", "step": max([s.index for s in asm.steps] or [1]) + 1,
                    "parts": [c.name for c in asm.cables], "in_bom": "cable" not in cats})
    return out


# ================================================================== purchased rows (shared by plan + bom)
CAT_ORDER = ["actuator", "pcb", "electronics", "sensor", "connector", "cable", "mechanical", "fastener", "fabricated"]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _catalog_name_index():
    idx = {}
    for table in (catalog.ACTUATORS, catalog.MODULES, catalog.BEARINGS):
        for k, v in table.items():
            idx[_norm(v.get("name", k))] = k
            idx[_norm(k)] = k
    return idx


def _select_vendor(vendors):
    if not vendors:
        return None
    fast = [v for v in vendors if (v.get("lead_days") or 0) <= 7]
    return min(fast or vendors, key=lambda v: v.get("unit_price", 0.0))


def _row(item, category, qty, vendors, how="buy from vendor", make="buy", **extra):
    sel = _select_vendor(vendors)
    unit = sel["unit_price"] if sel else 0.0
    return {"item": item, "category": category, "qty": int(qty), "make_or_buy": make, "how": how,
            "vendors": vendors, "selected": {"vendor": sel["vendor"], "unit_price": _r(unit)} if sel else None,
            "ext_price": _r(unit * qty), **extra}


def _category_for(sku):
    if sku in catalog.ACTUATORS:
        return "actuator"
    if sku in catalog.BEARINGS:
        return "mechanical"
    m = catalog.MODULES.get(sku)
    if m:
        role = m.get("role")
        if role == "connector":
            return "connector"
        if sku == "morph_controller_pcba":
            return "pcb"
        return "electronics"
    return "mechanical"


def buy_rows(design, asm, efinal=None, elec_entries=None) -> list:
    """Every off-the-shelf line for one robot: assembly purchased parts (by sku) merged with the electronics
    stage's sourcing rows (deduped by sku), fasteners by spec, cables when no harness is planned."""
    inst = _instances(asm)
    qty = Counter()
    labels = {}
    for e in inst.values():
        qty[e["sku"]] += e["n"]
        labels.setdefault(e["sku"], e.get("label") or "")
    for k, n in actuator_counts(asm).items():
        if k in catalog.ACTUATORS:
            qty[k] = max(qty[k], n)
    rows = OrderedDict()
    hardware = Counter()
    for sku, n in qty.items():
        how = "buy from vendor"
        try:
            c = catalog.get(sku)
        except KeyError:
            base = next((k for k in catalog.ACTUATORS if sku.startswith(k + "_") and "horn" in sku), None)
            hw = parse_fastener(sku)
            if base:                                 # servo horn: ships with the servo
                rows[sku] = {"item": f"{_short_name(catalog.ACTUATORS[base]['name'])} horn + horn screw", "category": "mechanical",
                             "qty": int(n), "make_or_buy": "buy", "how": f"included with the {_short_name(catalog.ACTUATORS[base]['name'])}",
                             "vendors": [], "selected": {"vendor": "included", "unit_price": 0.0}, "ext_price": 0.0,
                             "sku": sku, "source": "assembly"}
                continue
            if hw["size"] and hw["kind"] in ("standoff", "insert", "nut", "washer", "setscrew", "shcs", "bhcs", "fhcs"):
                hardware[hw["key"]] += n
                continue
            c = _GENERIC.get(next((g for g in _GENERIC if g in sku.lower()), ""), None) or \
                {"name": labels.get(sku) or pretty(sku),
                 "vendors": [catalog.V("amazon", labels.get(sku) or pretty(sku), 0.5, note="not in catalog: placeholder estimate")]}
        if sku == "morph_controller_pcba":
            how = "order PCBA from JLCPCB with jlc_bom.csv/jlc_cpl.csv"
        rows[sku] = _row(c.get("name", sku), _category_for(sku), n, [dict(v) for v in c.get("vendors", [])], how,
                         sku=sku, source="assembly")
    # electronics sourcing (authoritative for electronics; may also carry actuators / cables / connectors)
    idx = _catalog_name_index()
    elec = []
    for r in (efinal or {}).get("sourcing") or []:
        r = dict(r)
        sku = r.get("sku") or r.get("key") or idx.get(_norm(r.get("item", "")))
        if not sku:
            nm = _norm(r.get("item", ""))
            sku = next((k for n_, k in idx.items() if len(n_) > 6 and (nm.startswith(n_) or n_.startswith(nm))), None)
        r.setdefault("vendors", [])
        r.setdefault("make_or_buy", "buy")
        r.setdefault("how", "buy from vendor")
        r.setdefault("qty", 1)
        r.setdefault("category", "electronics")
        if sku and sku in rows:
            a = rows.pop(sku)
            if a["qty"] > r["qty"]:
                r["qty"] = a["qty"]
            if not r["vendors"]:
                r["vendors"] = a["vendors"]
        sel = r.get("selected") or _select_vendor(r["vendors"])
        if sel:
            r["selected"] = {"vendor": sel.get("vendor"), "unit_price": _r(sel.get("unit_price", 0.0))}
            r["ext_price"] = _r(r["selected"]["unit_price"] * r["qty"])
        else:
            r["ext_price"] = _r(r.get("ext_price", 0.0))
        r["sku"] = sku or ""
        r["source"] = "electronics"
        elec.append(r)
    out = list(rows.values()) + elec
    # fasteners by spec, over every step (+ hardware that the assembly models as purchased parts, e.g. standoffs)
    tot = OrderedDict()
    for st, fl in sorted(step_fasteners(asm).items()):
        for f in fl:
            cur = tot.get(f["key"])
            tot[f["key"]] = {**f, "qty": (cur["qty"] if cur else 0) + f["qty"]}
    for key, n in hardware.items():
        f = parse_fastener(key)
        cur = tot.get(f["key"])
        tot[f["key"]] = {**f, "qty": max(cur["qty"] if cur else 0, n), "source": "assembly"}
    have_fast = {_norm(r["item"]) for r in elec if r.get("category") == "fastener"}
    for f in tot.values():
        if f["kind"] == "other" or _norm(f["label"]) in have_fast:
            continue
        out.append(_row(f["label"], "fastener", f["qty"], fastener_vendors(f),
                        "buy from vendor" + (" (+10 % spares)" if f["qty"] >= 8 else ""),
                        spec=f["spec"], torque_nm=f["torque_nm"], source=f.get("source", "")))
    # cables, unless the electronics stage sources them or a harness is planned
    has_cable = any(r.get("category") == "cable" for r in elec) or any(
        e["process"] == "cable_assembly" for e in (elec_entries or []))
    if asm.cables and not has_cable:
        lengths = [asm.cable_length(c) for c in asm.cables]
        n = len(asm.cables)
        out.append(_row(f"Servo bus / signal cable set ({n} runs, {sum(lengths) / 1000:.2f} m)", "cable", n,
                        [catalog.V("amazon", "servo extension cable 3 pin 200mm", 0.9),
                         catalog.V("aliexpress", "5264 3pin servo cable 200mm", 0.45, lead_days=14)],
                        "buy pre-crimped cables; route along the link channels", source="assembly"))
    order = {c: i for i, c in enumerate(CAT_ORDER)}
    out.sort(key=lambda r: order.get(r.get("category"), 50))
    return out


def _volume_discount(q: int) -> float:
    return 1.0 if q < 10 else 0.95 if q < 100 else 0.88 if q < 1000 else 0.80


# ================================================================== DXF (R12) + SVG previews
class DXF:
    """Minimal AutoCAD R12 (AC1009) writer: POLYLINE/VERTEX/SEQEND (closed), LINE, CIRCLE, TEXT on named layers.
    R12 has no LWPOLYLINE; R12 is what laser / CNC / sheet-metal shops accept everywhere."""

    LAYERS = {"CUT": (1, "CONTINUOUS"), "BEND": (5, "DASHED"), "ENGRAVE": (3, "CONTINUOUS"), "NOTES": (8, "CONTINUOUS")}

    def __init__(self):
        self.ents = []
        self.pts = []

    def _ext(self, xy):
        self.pts.append(xy)

    def polyline(self, pts, layer="CUT", closed=True):
        pts = [(float(x), float(y)) for x, y in pts]
        if len(pts) > 1 and closed and np.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9:
            pts = pts[:-1]
        e = ["0", "POLYLINE", "8", layer, "66", "1", "70", "1" if closed else "0", "10", "0.0", "20", "0.0", "30", "0.0"]
        for x, y in pts:
            e += ["0", "VERTEX", "8", layer, "10", f"{x:.4f}", "20", f"{y:.4f}", "30", "0.0"]
            self._ext((x, y))
        e += ["0", "SEQEND", "8", layer]
        self.ents.append(e)

    def line(self, a, b, layer="BEND"):
        ltype = self.LAYERS.get(layer, (7, "CONTINUOUS"))[1]
        self.ents.append(["0", "LINE", "8", layer, "6", ltype, "10", f"{a[0]:.4f}", "20", f"{a[1]:.4f}", "30", "0.0",
                          "11", f"{b[0]:.4f}", "21", f"{b[1]:.4f}", "31", "0.0"])
        self._ext(a)
        self._ext(b)

    def circle(self, c, r, layer="CUT"):
        self.ents.append(["0", "CIRCLE", "8", layer, "10", f"{c[0]:.4f}", "20", f"{c[1]:.4f}", "30", "0.0", "40", f"{r:.4f}"])
        self._ext((c[0] - r, c[1] - r))
        self._ext((c[0] + r, c[1] + r))

    def text(self, p, h, s, layer="ENGRAVE"):
        s = str(s).encode("ascii", "replace").decode()
        self.ents.append(["0", "TEXT", "8", layer, "10", f"{p[0]:.4f}", "20", f"{p[1]:.4f}", "30", "0.0", "40", f"{h:.3f}", "1", s])

    def dumps(self) -> str:
        P = np.asarray(self.pts or [(0, 0)], float)
        lo, hi = P.min(0), P.max(0)
        H = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
             "9", "$MEASUREMENT", "70", "1", "9", "$EXTMIN", "10", f"{lo[0]:.4f}", "20", f"{lo[1]:.4f}", "30", "0.0",
             "9", "$EXTMAX", "10", f"{hi[0]:.4f}", "20", f"{hi[1]:.4f}", "30", "0.0", "0", "ENDSEC"]
        T = ["0", "SECTION", "2", "TABLES",
             "0", "TABLE", "2", "LTYPE", "70", "2",
             "0", "LTYPE", "2", "CONTINUOUS", "70", "0", "3", "Solid line", "72", "65", "73", "0", "40", "0.0",
             "0", "LTYPE", "2", "DASHED", "70", "0", "3", "__ __ __", "72", "65", "73", "2", "40", "6.0", "49", "4.0", "49", "-2.0",
             "0", "ENDTAB", "0", "TABLE", "2", "LAYER", "70", str(len(self.LAYERS))]
        for name, (color, lt) in self.LAYERS.items():
            T += ["0", "LAYER", "2", name, "70", "0", "62", str(color), "6", lt]
        T += ["0", "ENDTAB", "0", "ENDSEC"]
        E = ["0", "SECTION", "2", "ENTITIES"] + [x for e in self.ents for x in e] + ["0", "ENDSEC", "0", "EOF"]
        return "\n".join(H + T + E) + "\n"

    def save(self, path):
        with open(path, "w", newline="\r\n", encoding="utf-8") as fh:
            fh.write(self.dumps())
        return path


def _ring_d(coords):
    return "M" + " L".join(f"{x:.2f},{-y:.2f}" for x, y in coords) + " Z"


def _profile_svg(path, poly, title, bends=(), holes_round=(), sub="", w_px=520):
    b = poly.bounds
    pad = 14.0
    W, H = b[2] - b[0] + 2 * pad, b[3] - b[1] + 2 * pad + 12
    vb = f"{b[0] - pad:.2f} {-b[3] - pad:.2f} {W:.2f} {H:.2f}"
    rings = [poly.exterior, *poly.interiors]
    d = " ".join(_ring_d(r.coords) for r in rings)
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w_px}" height="{w_px * H / W:.0f}" '
         f'font-family="Helvetica, Arial, sans-serif">',
         f'<rect x="{b[0] - pad:.2f}" y="{-b[3] - pad:.2f}" width="{W:.2f}" height="{H:.2f}" fill="#f7f8fa"/>',
         f'<path d="{d}" fill="#dfe6ee" fill-rule="evenodd" stroke="#d6293e" stroke-width="{max(W, H) / 400:.3f}"/>']
    for i, bd in enumerate(bends):
        (u0, v0), (u1, v1) = bd["line"]
        s.append(f'<line x1="{u0:.2f}" y1="{-v0:.2f}" x2="{u1:.2f}" y2="{-v1:.2f}" stroke="#2b6cd4" '
                 f'stroke-width="{max(W, H) / 450:.3f}" stroke-dasharray="{max(W, H) / 60:.2f} {max(W, H) / 120:.2f}"/>')
        mx, my = (u0 + u1) / 2, (v0 + v1) / 2
        L = math.hypot(u1 - u0, v1 - v0) or 1.0
        nx, ny = -(v1 - v0) / L, (u1 - u0) / L                 # label sits beside the line, rotated along it
        off = max(W, H) / 70
        ang = -math.degrees(math.atan2(v1 - v0, u1 - u0))
        ang = ang + 180 if ang <= -90 or ang > 90 else ang
        tx, ty = mx + nx * off, -(my + ny * off)
        s.append(f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{max(W, H) / 40:.2f}" fill="#2b6cd4" text-anchor="middle" '
                 f'dominant-baseline="middle" transform="rotate({ang:.1f} {tx:.2f} {ty:.2f})">'
                 f'B{i + 1} {"UP" if bd.get("angle_deg", 90) >= 0 else "DN"} {abs(bd.get("angle_deg", 90)):g}° R{bd.get("radius", 1):g}</text>')
    s.append(f'<text x="{b[0]:.2f}" y="{-b[1] + pad * 0.9:.2f}" font-size="{max(W, H) / 32:.2f}" fill="#36404c">{title}</text>')
    if sub:
        s.append(f'<text x="{b[0]:.2f}" y="{-b[1] + pad * 0.9 + max(W, H) / 26:.2f}" font-size="{max(W, H) / 44:.2f}" fill="#7a8592">{sub}</text>')
    s.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(s))
    return path


def write_profile_dxf(path, poly, label, bends=(), round_holes=True):
    dxf = DXF()
    rnd, c, r = _round_ring(poly.exterior, 24)
    if round_holes and rnd:
        dxf.circle(c, r, "CUT")
    else:
        dxf.polyline(np.asarray(poly.exterior.coords), "CUT")
    for ring in poly.interiors:
        rnd, cc, rh = _round_ring(ring)
        if round_holes and rnd:
            dxf.circle(cc, rh, "CUT")
        else:
            dxf.polyline(np.asarray(ring.coords), "CUT")
    for i, bd in enumerate(bends):
        (u0, v0), (u1, v1) = bd["line"]
        dxf.line((u0, v0), (u1, v1), "BEND")
        ang = bd.get("angle_deg", 90)
        dxf.text(((u0 + u1) / 2, (v0 + v1) / 2 + 1.0), 2.0,
                 f"B{i + 1} {'UP' if ang >= 0 else 'DOWN'} {abs(ang):g} R{bd.get('radius', 1):g}", "BEND")
    b = poly.bounds
    h = max(1.5, min(4.0, (b[3] - b[1]) * 0.06))
    rp = poly.representative_point()
    inner = poly.buffer(-h * 1.2)
    at = (rp.x - len(label) * h * 0.3, rp.y - h / 2) if not inner.is_empty else (b[0], b[3] + h)
    dxf.text(at, h, label, "ENGRAVE")
    return dxf.save(path)


# ================================================================== G-code (2.5D: holes, contour, tabs)
CNC_TOOLS = {   # 3-axis VMC / rigid desktop mill; carbide tooling
    "Al6061": {"T1": dict(d=6.0, flutes=3, rpm=12000, chip=0.035, doc=2.0, plunge=300),
               "T2": dict(d=3.0, flutes=2, rpm=18000, chip=0.018, doc=1.0, plunge=200)},
    "Delrin": {"T1": dict(d=6.0, flutes=2, rpm=12000, chip=0.08, doc=3.0, plunge=500),
               "T2": dict(d=3.0, flutes=2, rpm=16000, chip=0.04, doc=1.5, plunge=400)},
}


class CamWriter(GcodeWriter):
    """fab.GcodeWriter + per-tool feeds, helical arcs (G2/G3) and time accounting across tool changes."""

    def __init__(self):
        super().__init__(1000.0, 300.0)
        self._t = 0.0

    def set_feeds(self, feed, plunge):
        self._t += self.cut_mm / self.feed + self.plunge_mm / self.plunge
        self.cut_mm = self.plunge_mm = 0.0
        self.feed, self.plunge = float(feed), float(plunge)

    def arc(self, x, y, z, i, j, cw=True):
        r = math.hypot(i, j)
        dz = abs(z - self.pos[2])
        self.cut_mm += math.hypot(2 * math.pi * r, dz)
        self.emit(f"{'G2' if cw else 'G3'} X{x:.3f} Y{y:.3f} Z{z:.3f} I{i:.3f} J{j:.3f} F{self.feed:.0f}")
        self.pos = np.array([x, y, z], float)

    def minutes(self):
        return self._t + super().minutes()


def _resample(coords, step=1.0):
    line = LineString(coords)
    n = max(int(line.length / step), 8)
    return [line.interpolate(i / n, normalized=True) for i in range(n + 1)], line.length


def write_gcode(nc_path, poly, thickness, material, label, svg_path=None):
    """Plate program: drill/helix holes, then the outside contour (climb, multi-pass) with holding tabs.
    Stock = profile bounds + 5 mm, origin lower-left of the stock, Z0 = stock top."""
    tools = CNC_TOOLS.get(material, CNC_TOOLS["Al6061"])
    margin = 5.0
    b = poly.bounds
    part = affinity.translate(poly, margin + 3.0 - b[0], margin + 3.0 - b[1])
    sx, sy = b[2] - b[0] + 2 * (margin + 3.0), b[3] - b[1] + 2 * (margin + 3.0)
    depth = thickness + 0.25
    g = CamWriter()
    g.emit(f"( Morph CNC program: {label} )")
    g.emit(f"( material {material}, plate {thickness:.2f} mm; stock {sx:.1f} x {sy:.1f} mm )")
    g.emit("( WCS G54: X0 Y0 = stock lower-left corner, Z0 = stock top. Fixture: vacuum or double-sided tape + tabs )")
    for tk, t in tools.items():
        g.emit(f"( {tk}: {t['d']:g} mm {t['flutes']}-flute carbide end mill, S{t['rpm']} )")
    g.emit("( Fanuc/LinuxCNC/GRBL compatible: no canned cycles, tool changes as M0 pauses )")
    g.emit("G21 G90 G17 G94 G54")
    previews = []
    holes, slots = [], []
    for ring in part.interiors:
        rnd, c, rr = _round_ring(ring)
        (holes if rnd else slots).append((c[0], c[1], 2 * rr, ring))
    t2 = tools["T2"]
    t1 = tools["T1"]
    small = [h for h in holes if h[2] < t1["d"] + 0.5]
    big = [h for h in holes if h[2] >= t1["d"] + 0.5] + slots

    def start_tool(name, t):
        feed = t["rpm"] * t["flutes"] * t["chip"]
        g.set_feeds(feed, t["plunge"])
        g.emit(f"( --- {name}: {t['d']:g} mm end mill, feed {feed:.0f} mm/min --- )")
        g.emit("M5")
        g.emit(f"M0 ( load {name} {t['d']:g} mm end mill, touch off Z0 on stock top )")
        g.emit(f"M3 S{t['rpm']}")
        g.rapid(z=SAFE_Z)

    def helix(cx, cy, dia, t):
        r = t["d"] / 2
        pr = dia / 2 - r
        if pr < 0.15:          # hole ~ tool size: peck-plunge (expanded G83)
            g.emit(f"( peck hole d={dia:.2f} at X{cx:.2f} Y{cy:.2f} )")
            g.rapid(cx, cy)
            g.rapid(z=1.0)
            z = 0.0
            while z > -depth + 1e-6:
                z = max(z - t["doc"], -depth)
                g.cut(cx, cy, z)
                g.rapid(z=1.0)
            g.rapid(z=SAFE_Z)
            previews.append(Point(cx, cy).buffer(dia / 2).exterior)
            return
        g.emit(f"( helical hole d={dia:.2f} at X{cx:.2f} Y{cy:.2f}, pitch 0.6 )")
        g.rapid(cx + pr, cy)
        g.rapid(z=0.5)
        z = 0.0
        g.cut(cx + pr, cy, 0.0)
        while z > -depth + 1e-6:
            z = max(z - 0.6, -depth)
            g.arc(cx + pr, cy, z, -pr, 0.0, cw=False)
        g.arc(cx + pr, cy, -depth, -pr, 0.0, cw=False)
        g.cut(cx, cy, -depth)
        g.rapid(z=SAFE_Z)
        previews.append(Point(cx, cy).buffer(pr).exterior)

    if small:
        start_tool("T2", t2)
        for cx, cy, d, ring in small:
            if d < t2["d"] - 0.05:
                g.emit(f"( NOTE hole d={d:.2f} smaller than T2: spot here, drill {d:.2f} mm on the drill press )")
                g.rapid(cx, cy)
                g.rapid(z=1.0)
                g.cut(cx, cy, -0.5)
                g.rapid(z=SAFE_Z)
                continue
            helix(cx, cy, d, t2)
    start_tool("T1", t1)
    for cx, cy, d, ring in big:
        if _round_ring(ring)[0]:
            helix(cx, cy, d, t1)
        else:      # non-round cut-out: inside contour, conventional (CCW)
            cut = orient(Polygon(ring).buffer(-t1["d"] / 2, quad_segs=16), 1.0)
            if cut.is_empty:
                continue
            cut = cut if cut.geom_type == "Polygon" else max(cut.geoms, key=lambda q: q.area)
            pts, _ = _resample(cut.exterior.coords)
            g.emit("( inside cut-out )")
            g.rapid(pts[0].x, pts[0].y)
            g.rapid(z=1.0)
            nl = max(math.ceil(depth / t1["doc"]), 1)
            for k in range(nl):
                z = -depth * (k + 1) / nl
                for p in pts:
                    g.cut(p.x, p.y, z)
            g.rapid(z=SAFE_Z)
            previews.append(cut.exterior)
    # outside contour, climb (clockwise), tabs on the last 0.8 mm
    r = t1["d"] / 2
    contour = orient(Polygon(part.exterior).buffer(r, quad_segs=24), -1.0).exterior
    pts, perim = _resample(contour.coords)
    n_tabs = 2 if perim < 150 else 4 if perim < 600 else 6
    tabs = [perim * (k + 0.5) / n_tabs for k in range(n_tabs)]
    tab_h, tab_w = min(0.8, thickness * 0.3), 5.0
    nl = max(math.ceil(depth / t1["doc"]), 1)
    g.emit(f"( outside contour: {nl} passes, {n_tabs} tabs {tab_w:g} x {tab_h:g} mm )")
    g.rapid(pts[0].x, pts[0].y)
    g.rapid(z=1.0)
    for k in range(nl):
        z = -depth * (k + 1) / nl
        for i, p in enumerate(pts):
            s = perim * i / (len(pts) - 1)
            zz = z
            if z < -(depth - tab_h) and any(abs(s - t) < tab_w / 2 + r for t in tabs):
                zz = -(depth - tab_h)
            g.cut(p.x, p.y, zz)
    g.rapid(z=SAFE_Z)
    previews.append(contour)
    g.emit("M5")
    g.rapid(0, 0, 25.0)
    g.emit("M30")
    minutes = g.minutes() + 4.0
    g.lines.insert(1, f"( estimated cycle {minutes:.1f} min, {len(g.lines)} blocks )")
    with open(nc_path, "w", encoding="utf-8") as fh:
        fh.write("%\n" + "\n".join(g.lines) + "\n%\n")
    if svg_path:
        sw = max(sx, sy) / 350
        vw = max(sx + 8, 150.0)
        s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 {-sy - 4:.1f} {vw:.1f} {sy + 18:.1f}" '
             f'width="{min(900, vw * 3):.0f}" font-family="Helvetica, Arial, sans-serif">',
             f'<rect x="0" y="{-sy:.2f}" width="{sx:.2f}" height="{sy:.2f}" fill="#e9edf1" stroke="#9aa4ae" stroke-width="{sw:.2f}"/>',
             f'<path d="{" ".join(_ring_d(rg.coords) for rg in [part.exterior, *part.interiors])}" fill="#b9c6d4" fill-rule="evenodd"/>']
        for rg in previews:
            s.append(f'<path d="{_ring_d(rg.coords)}" fill="none" stroke="#d6293e" stroke-width="{sw:.2f}"/>')
        for t in tabs:
            p = LineString(contour.coords).interpolate(t)
            s.append(f'<rect x="{p.x - 2.5:.2f}" y="{-p.y - 2.5:.2f}" width="5" height="5" fill="#f2a516"/>')
        s.append(f'<text x="0" y="{12:.1f}" font-size="{min(5.0, vw / 34):.1f}" fill="#36404c">{label} · {minutes:.0f} min</text></svg>')
        with open(svg_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(s))
    return {"minutes": _r(minutes, 1), "blocks": len(g.lines), "tools": list(tools), "tabs": n_tabs, "passes": nl,
            "stock_mm": [_r(sx, 1), _r(sy, 1), _r(thickness, 2)]}


# ================================================================== 3MF / STL
def write_3mf(path, mesh, qty=1, title="", meta=None, bed=FDM_BED):
    v = np.asarray(mesh.vertices, float)
    f = np.asarray(mesh.faces, np.int64)
    verts = ('<vertex x="%.4f" y="%.4f" z="%.4f"/>' * len(v)) % tuple(v.ravel().tolist())     # one C-level format pass
    tris = ('<triangle v1="%d" v2="%d" v3="%d"/>' * len(f)) % tuple(f.ravel().tolist())
    ext = mesh.extents
    gap = 6.0
    cols = max(1, int((bed[0] + gap) // (ext[0] + gap)))
    items = []
    for k in range(qty):
        cx = (k % cols) * (ext[0] + gap) - (min(qty, cols) - 1) * (ext[0] + gap) / 2 + bed[0] / 2
        cy = (k // cols) * (ext[1] + gap) - ((qty - 1) // cols) * (ext[1] + gap) / 2 + bed[1] / 2
        items.append(f'<item objectid="1" transform="1 0 0 0 1 0 0 0 1 {cx:.3f} {cy:.3f} 0"/>')
    md = "".join(f'<metadata name="{k}">{_xml(str(val))}</metadata>' for k, val in
                 {"Title": title, "Application": "Morph", **(meta or {})}.items())
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'{md}<resources><object id="1" type="model" name="{_xml(title)}"><mesh><vertices>{verts}</vertices>'
             f'<triangles>{tris}</triangles></mesh></object></resources><build>{"".join(items)}</build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return path


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ================================================================== DFM checks
def dfm_checks(entry: dict, g: Geo) -> list:
    proc = entry["process"]
    out = []
    if proc in PRINT_PROCESSES:
        pose = g.pose_for(proc)
        bed = BEDS[proc]
        e = pose["mesh"].extents          # as it stands on the bed: footprint either way round, height against the bed's
        if e[2] > bed[2] or max(e[:2]) > max(bed[:2]) or min(e[:2]) > min(bed[:2]):
            out.append(f"{e[0]:.0f}×{e[1]:.0f}×{e[2]:.0f} mm (its best way up) exceeds the {bed[0]:.0f}×{bed[1]:.0f}×"
                       f"{bed[2]:.0f} mm build volume: split the part")
        w = g.walls
        lim = {"fdm_print": 1.2, "sla_print": 0.6, "mjf_print": 0.8}[proc]
        if w and w["min"] < lim:
            out.append(f"thin wall ≈{w['min']:.2f} mm (min {lim:g} mm for {PROC_LABEL[proc]}): thicken or accept a weak spot")
        if proc == "fdm_print":
            if pose["support_mm2"] > 25:
                out.append(f"{pose['support_mm2'] / 100:.1f} cm² of >45° overhang in the best orientation ({pose['label']}): "
                           f"tree supports, ~{pose['overhang_pct']:.0f} % of the surface")
            if pose["height"] > 4.0 * max(np.sort(pose["mesh"].extents)[:2].min(), 1.0) and pose["height"] > 40:
                out.append("tall and slender on the bed: add a brim, print slower above 40 mm")
        if proc == "sla_print":
            out.append("supports on the non-cosmetic side; post-cure 30 min at 60 °C before assembly")
        if entry["class"] == "gear" and proc == "fdm_print":
            out.append("gear teeth: print at 0.12 mm layers for mesh accuracy")
    elif proc in ("cnc_mill", "laser_cut", "cnc_turn"):
        pr = g.profile
        tool_r = 1.5 if proc == "cnc_mill" else 0.1
        if proc == "cnc_mill":
            closed = pr.buffer(tool_r, quad_segs=16).buffer(-tool_r, quad_segs=16)
            left = closed.area - pr.area
            if left > 0.5:
                out.append(f"inside corners sharper than the R{tool_r:g} mm tool leave {left:.1f} mm² uncut: add R≥{tool_r:g} fillets")
            if entry["class"] != "plate" and g.p.profile is None:
                out.append("3D features beyond the 2.5D outline need vendor CAM from the STEP/STL (program covers outline + holes)")
        t = g.thickness
        if proc != "cnc_turn" and not any(abs(t - s) < 0.05 for s in PLATE_STOCK):
            up = next((s for s in PLATE_STOCK if s >= t), PLATE_STOCK[-1])
            out.append(f"{t:.2f} mm is not stock plate: face down from {up:g} mm (+ one setup) or round to stock")
        for h in g.holes:
            if h["round"] and h["d"] < 3.0 and proc == "cnc_mill":
                out.append(f"Ø{h['d']:.2f} mm hole below the 3 mm end mill: drilled op (spot + drill)")
                break
        if proc == "laser_cut" and g.holes and min(h["d"] for h in g.holes) < g.thickness:
            out.append("hole diameter below the plate thickness: laser taper, drill to size")
        w = g.walls
        if w and w["min"] < 0.8 and entry["material"].startswith("Al"):
            out.append(f"thin web ≈{w['min']:.2f} mm: chatter / deflection risk in aluminium")
    elif proc == "sheet_metal":
        out += _sheet_dfm(g)
    elif proc == "injection_mold":
        mo = g.mold
        w = g.walls
        if mo["wall_mm2"] > 0:
            frac = mo["low_draft_mm2"] / mo["wall_mm2"]
            if frac > 0.05:
                out.append(f"{mo['low_draft_mm2'] / 100:.1f} cm² of side walls ({100 * frac:.0f} %) have <1° draft along the {mo['axis']} pull: "
                           "add 1–2° draft (3° under texture)")
        if w:
            if w["median"] > 4.0:
                out.append(f"thick section ≈{w['median']:.1f} mm: core out to 2–3 mm walls with ribs (sink marks, {12 + 2.5 * w['median'] ** 2:.0f} s cycle)")
            elif w["max"] > 2.5 * max(w["p10"], 0.5):
                out.append(f"wall varies {w['p10']:.1f}–{w['max']:.1f} mm: keep within ±25 % to avoid warp and sink")
            if w["min"] < 0.8:
                out.append(f"wall ≈{w['min']:.2f} mm is below the 0.8 mm fill limit for {entry['material']}")
        if mo["side_actions"]:
            out.append(f"undercuts need ≈{mo['side_actions']} side action(s) (+$1,400 each): redesign to pass-through cores if possible")
    return out


def _down1(v: float) -> float:
    """A measured length (mm) shown to 0.1 mm rounded down, so one short of a minimum never reads as the minimum."""
    return math.floor(float(v) * 10.0 + 1e-6) / 10.0


def _sheet_dfm(g: Geo) -> list:
    p = g.p
    sheet = p.meta.get("sheet") or {}
    t = float(sheet.get("thickness", g.thickness))
    pr = g.profile
    out = []
    if not any(abs(t - s) < 0.05 for s in SHEET_GAUGES):
        near = min(SHEET_GAUGES, key=lambda s: abs(s - t))
        out.append(f"{t:.2f} mm is not a stock gauge: use {near:g} mm")
    for i, bd in enumerate(sheet.get("bends", [])):
        R = float(bd.get("radius", t))
        (u0, v0), (u1, v1) = bd["line"]
        ln = LineString([(u0, v0), (u1, v1)])
        mat = str(sheet.get("material") or p.material)
        rmin_f = {"Al6061": 2.0, "Al7075": 3.0, "Stainless": 0.8}.get(mat, 0.5)    # 5052-H32 / mild steel ~0.5 T
        if R < t * rmin_f * 0.99:
            out.append(f"B{i + 1}: inside radius {R:g} mm is below {rmin_f:g}×T ({t * rmin_f:.1f} mm) for {mat}: "
                       "the outside of the bend can crack")
        # flange lengths: farthest outline point on each side of the (extended) bend line
        d = np.array([u1 - u0, v1 - v0], float)
        L = np.linalg.norm(d)
        if L < 1e-9:
            continue
        nrm = np.array([-d[1], d[0]]) / L
        pts = np.asarray(pr.exterior.coords)
        side = (pts - np.array([u0, v0])) @ nrm
        min_fl = max(4.0 * t, 3.0 * t + R)
        for sgn, nm in ((1, "a"), (-1, "b")):
            reach = float(max(side * sgn))
            if reach < min_fl:
                out.append(f"B{i + 1}: flange {nm} is {_down1(reach):.1f} mm, below the {min_fl:.1f} mm press-brake minimum "
                           "(4×T)")
        # bend relief at the ends where the bend meets the outline
        for (x, y) in ((u0, v0), (u1, v1)):
            disk = Point(x, y).buffer(max(t, 1.0), quad_segs=16)
            frac = pr.intersection(disk).area / disk.area
            if frac > 0.6:
                out.append(f"B{i + 1}: no bend relief at ({x:.0f}, {y:.0f}): add a {t:g}×{t + R:g} mm relief notch to stop tearing")
                break
        for h in g.holes:
            dist = ln.distance(Point(h["c"])) - h["d"] / 2
            if dist < 2.0 * t + R:
                where = ("crosses the bend line" if dist < 0 else
                         f"sits {_down1(dist):.1f} mm from the bend (min {2 * t + R:.1f})")
                out.append(f"B{i + 1}: hole Ø{h['d']:.1f} {where}: it will distort")
                break
    return out


# ================================================================== machine files per process
def _bend_table(path, sheet, poly):
    t = float(sheet.get("thickness", 2.0))
    k = float(sheet.get("k_factor", 0.44))
    rows = []
    for i, bd in enumerate(sheet.get("bends", [])):
        ang = float(bd.get("angle_deg", 90.0))
        R = float(bd.get("radius", t))
        a = abs(ang)
        ba = math.pi / 180.0 * a * (R + k * t)
        ossb = math.tan(math.radians(a) / 2) * (R + t)
        bd_ = 2 * ossb - ba
        (u0, v0), (u1, v1) = bd["line"]
        rows.append({"bend": f"B{i + 1}", "sequence": i + 1, "direction": "up" if ang >= 0 else "down", "angle_deg": _r(a, 2),
                     "inside_radius_mm": _r(R, 3), "thickness_mm": _r(t, 3), "k_factor": _r(k, 3),
                     "bend_allowance_mm": _r(ba, 3), "outside_setback_mm": _r(ossb, 3), "bend_deduction_mm": _r(bd_, 3),
                     "line_u0": _r(u0, 3), "line_v0": _r(v0, 3), "line_u1": _r(u1, 3), "line_v1": _r(v1, 3),
                     "length_mm": _r(math.hypot(u1 - u0, v1 - v0), 2)})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        cols = list(rows[0]) if rows else ["bend", "angle_deg", "inside_radius_mm", "k_factor", "bend_allowance_mm", "bend_deduction_mm"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return rows


def write_files(entry: dict, g: Group, outdir: str, design, efinal=None) -> list:
    proc = entry["process"]
    key = g.key
    d = os.path.join(outdir, key)
    os.makedirs(d, exist_ok=True)
    files = []
    geo = g.geo

    def rel(fn):
        files.append(f"{key}/{fn}")
        return os.path.join(d, fn)

    if proc in PRINT_PROCESSES:
        pose = geo.pose_for(proc)
        pm = pose["mesh"]
        pm.export(rel(f"{key}.stl"))
        settings = {"fdm_print": {"layer_height_mm": 0.2 if g.cls != "gear" else 0.12, "perimeters": 4, "top_bottom_layers": 5,
                                  "infill": "25 % gyroid", "supports": "tree, buildplate only" if pose["support_mm2"] > 25 else "none",
                                  "nozzle_c": 240 if entry["material"] == "PETG" else 215, "bed_c": 80 if entry["material"] == "PETG" else 60},
                    "sla_print": {"layer_height_mm": 0.05, "resin": "Tough 2000 class", "supports": "auto, non-cosmetic side",
                                  "post_cure": "30 min @ 60 °C"},
                    "mjf_print": {"material": "HP 3D HR PA12", "finish": "bead blast + dye black", "tolerance": "±0.3 mm"}}[proc]
        write_3mf(rel(f"{key}.3mf"), pm, qty=g.qty, title=entry["name"],
                  meta={"Designer": "Morph", "Description": f"{entry['material']} {PROC_LABEL[proc]} · orientation: {pose['label']}",
                        **{f"morph:{k}": v for k, v in settings.items()}},
                  bed=BEDS[proc])
        entry["print"] = {"orientation": pose["label"], "height_mm": _r(pose["height"], 1),
                          "support_cm2": _r(pose["support_mm2"] / 100, 2), **settings}
        if proc == "fdm_print":
            used, grams, hours = _fdm_numbers(geo, entry["material"])
            entry["print"].update({"grams": _r(grams * 1, 1), "hours": _r(hours, 2)})
    elif proc in ("cnc_mill", "laser_cut", "cnc_turn"):
        pr = geo.profile
        label = f"{key.upper()} {entry['material']} {geo.thickness:g}mm QTY{entry['qty']}"
        write_profile_dxf(rel(f"{key}.dxf"), pr, label)
        _profile_svg(rel(f"{key}.svg"), pr, entry["name"], sub=f"{entry['material']} · {geo.thickness:g} mm · {PROC_LABEL[proc]}")
        if proc in ("cnc_mill", "cnc_turn"):
            entry["cnc"] = write_gcode(rel(f"{key}.nc"), pr, geo.thickness, entry["material"], label,
                                       svg_path=rel(f"{key}_toolpath.svg"))
            if entry["class"] != "plate":
                g.rep.mesh.export(rel(f"{key}.stl"))
    elif proc == "sheet_metal":
        sheet = g.rep.meta.get("sheet") or {"thickness": geo.thickness, "bends": [], "k_factor": 0.44}
        pr = geo.profile
        label = f"{key.upper()} {entry['material']} {sheet.get('thickness', geo.thickness):g}mm QTY{entry['qty']}"
        write_profile_dxf(rel(f"{key}_flat.dxf"), pr, label, bends=sheet.get("bends", []))
        rows = _bend_table(rel(f"{key}_bends.csv"), sheet, pr)
        _profile_svg(rel(f"{key}_flat.svg"), pr, entry["name"] + " — flat pattern", bends=sheet.get("bends", []),
                     sub=f"{entry['material']} {sheet.get('thickness', geo.thickness):g} mm · K {sheet.get('k_factor', 0.44):g} · "
                         f"{len(rows)} bends")
        g.rep.mesh.export(rel(f"{key}.stl"))
        entry["sheet"] = {"thickness_mm": sheet.get("thickness"), "k_factor": sheet.get("k_factor", 0.44), "bends": rows}
    elif proc == "injection_mold":
        g.rep.mesh.export(rel(f"{key}.stl"))
        mo = geo.mold
        w = geo.walls or {}
        sel = next((q for q in entry["quotes"] if q["vendor"] == entry["vendor"]), entry["quotes"][0])
        notes = {
            "part": entry["name"], "material": entry["material"], "qty_per_robot": entry["qty"],
            "pull_direction": mo["axis"], "parting_line": f"at the largest silhouette normal to {mo['axis']}",
            "draft": {"walls_cm2": _r(mo["wall_mm2"] / 100, 1), "below_1deg_cm2": _r(mo["low_draft_mm2"] / 100, 1),
                      "target": "1–2° (3° per 0.025 mm texture depth)"},
            "wall_mm": {k: _r(v, 2) for k, v in w.items()} or None, "nominal_wall_target_mm": "2.0–3.0 (PC/ABS)",
            "gate": "edge/tab gate on the parting line at the thickest section, hidden face; cold-slug well",
            "ejection": "pins on the core side under ribs/bosses; no pins on cosmetic faces",
            "side_actions": mo["side_actions"], "cavities": 1 if (entry["qty"] * design.build.quantity) < 5000 else 2,
            "tool": {"vendor": entry["vendor"], "cost_usd": entry["tooling_cost"], "lead_days": entry["lead_days"],
                     "type": sel.get("note", "")}, "cost_model": entry["cost_model"],
            "dfm": entry.get("dfm", [])}
        with open(rel(f"{key}_molding.json"), "w", encoding="utf-8") as fh:
            json.dump(notes, fh, indent=1)
        lines = [f"INJECTION MOLDING NOTES — {entry['name']} ({key})", "=" * 60,
                 f"Material ........ {entry['material']}", f"Qty per robot ... {entry['qty']}",
                 f"Pull direction .. {mo['axis']} (parting line at the largest silhouette)",
                 f"Draft ........... target 1–2°; {mo['low_draft_mm2'] / 100:.1f} of {mo['wall_mm2'] / 100:.1f} cm² of walls are below 1°",
                 f"Walls ........... median {w.get('median', float('nan')):.2f} mm (min {w.get('min', float('nan')):.2f}, "
                 f"max {w.get('max', float('nan')):.2f}); target 2.0–3.0 mm uniform",
                 f"Gate ............ {notes['gate']}", f"Ejection ........ {notes['ejection']}",
                 f"Side actions .... {mo['side_actions']}", f"Cavities ........ {notes['cavities']}",
                 f"Tool estimate ... ${entry['tooling_cost']:,.0f} ({entry['vendor']}, {sel.get('note', '')}), lead {entry['lead_days']} d",
                 f"Piece price ..... ${entry['unit_cost']:.2f} at {entry['qty'] * design.build.quantity} pcs",
                 "", "Cost model: " + entry["cost_model"]["formula"], "Inputs: " + json.dumps(entry["cost_model"]["inputs"]),
                 "", "DFM:"] + [f"  - {x}" for x in entry.get("dfm", [])] + ["", "Send the STEP from CAD (STL here is for reference) for a binding DFM review."]
        with open(rel(f"{key}_molding.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return files


def _write_elec_files(entry, outdir, efinal):
    d = os.path.join(outdir, entry["group"])
    os.makedirs(d, exist_ok=True)
    files = []
    if entry["process"] == "pcb_assembly":
        edir = _elec_dir(outdir, efinal)
        board = (efinal or {}).get("board") or {}
        cands = [board.get("gerbers"), "board/jlc_bom.csv", "board/jlc_cpl.csv", board.get("json")]
        for rel in [c for c in cands if c]:
            src = os.path.join(edir, rel)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(d, os.path.basename(rel)))
                files.append(f"{entry['group']}/{os.path.basename(rel)}")
        if not files:
            entry["notes"] = "gerbers / JLC BOM + CPL are produced by the electronics stage (electronics/board/)"
    elif entry["process"] == "cable_assembly":
        harness = (efinal or {}).get("harness") or []
        p = os.path.join(d, "harness_cutlist.csv")
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["wire", "from", "to", "cut_length_mm", "conductors", "wire_type", "connector_a", "connector_b",
                        "strip_mm", "crimps"])
            for i, h in enumerate(harness):
                c = int(h.get("conductors", 2) or 2)
                w.writerow([h.get("name", f"W{i + 1}"), h.get("from", ""), h.get("to", ""),
                            int(round(float(h.get("length_mm", 0)) + 20)), c, h.get("wire", ""), h.get("connector_a", ""),
                            h.get("connector_b", ""), 2.5, 2 * c])
        files.append(f"{entry['group']}/harness_cutlist.csv")
        svg = _harness_svg(harness, entry["name"])
        with open(os.path.join(d, "harness.svg"), "w", encoding="utf-8") as fh:
            fh.write(svg)
        files.append(f"{entry['group']}/harness.svg")
    return files


def _harness_svg(harness, title):
    rowh = 34
    W = 900
    H = 70 + rowh * max(len(harness), 1)
    maxL = max([float(h.get("length_mm", 100) or 100) for h in harness] or [100])
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#fbfbfc"/>',
         f'<text x="20" y="32" font-size="18" font-weight="600" fill="#1d2430">{_xml(title)} — formboard (1 row per wire)</text>']
    for i, h in enumerate(harness):
        y = 64 + i * rowh
        L = float(h.get("length_mm", 100) or 100)
        x0, x1 = 230, 230 + (W - 380) * L / maxL
        c = int(h.get("conductors", 2) or 2)
        s.append(f'<text x="20" y="{y + 5}" font-size="12" fill="#1d2430">{_xml(str(h.get("name", "")))[:34]}</text>')
        for k in range(c):
            s.append(f'<line x1="{x0}" y1="{y - 4 + k * 3}" x2="{x1:.1f}" y2="{y - 4 + k * 3}" stroke="{["#c0392b", "#1d2430", "#e6b800", "#2d7be5"][k % 4]}" stroke-width="2"/>')
        s.append(f'<rect x="{x0 - 16}" y="{y - 9}" width="16" height="16" rx="2" fill="#efe6cf" stroke="#9a8f74"/>')
        s.append(f'<rect x="{x1:.1f}" y="{y - 9}" width="16" height="16" rx="2" fill="#efe6cf" stroke="#9a8f74"/>')
        s.append(f'<text x="{x1 + 24:.1f}" y="{y + 5}" font-size="11" fill="#55606c">{L:.0f} mm · {_xml(str(h.get("connector_a", "")))} → {_xml(str(h.get("connector_b", "")))}</text>')
    s.append("</svg>")
    return "\n".join(s)


# ================================================================== plan
def _config_summary(cfg, entries, nq, tooling):
    by = Counter()
    for e in entries:
        if e["process"] in FAB_PROCESSES:
            by[(e["process"], e["material"])] += e["qty"]
    parts = []
    for (proc, mat), n in by.most_common():
        lab = {"fdm_print": f"FDM {mat}", "sla_print": "SLA resin", "mjf_print": "MJF PA12", "cnc_mill": f"CNC {mat}",
               "cnc_turn": f"turned {mat}", "laser_cut": f"laser-cut {mat}", "sheet_metal": f"{mat} sheet metal",
               "injection_mold": f"molded {mat}"}.get(proc, proc)
        parts.append(f"{lab} ×{n}")
    vend = Counter(e["vendor"] for e in entries if e["process"] in FAB_PROCESSES)
    s = ", ".join(parts[:4])
    if vend:
        s += " · " + ", ".join(v for v, _ in vend.most_common(3))
    if tooling:
        tools = sum(1 for e in entries if e.get("tooling_cost"))
        s += f" · {tools} mold tool{'s' if tools != 1 else ''} ${tooling:,.0f} over {nq:,} unit{'s' if nq != 1 else ''}"
    if any(e["process"] == "pcb_assembly" for e in entries):
        s += " · PCBA"
    return s


def _config_cost(entries, buy_total, nq):
    fab = sum(e["unit_cost"] * e["qty"] for e in entries if e.get("in_bom", True))
    tooling = sum(e.get("tooling_cost", 0.0) for e in entries)
    setup = sum(e.get("setup_cost", 0.0) for e in entries)
    unit = fab + buy_total + tooling / max(nq, 1)
    return _r(fab), _r(tooling), _r(setup), _r(unit)


def plan(design, asm, outdir, efinal=None) -> dict:
    os.makedirs(outdir, exist_ok=True)
    active = design.build.config if design.build.config in CONFIGS else "prototype"
    nq = max(1, int(design.build.quantity or 1))
    groups = fab_groups(asm)
    warnings, issues = [], []
    elec_proto = _elec_estimates(design, asm, efinal, nq)
    rows = buy_rows(design, asm, efinal, elec_proto)
    dvol = _volume_discount(nq)
    buy_total = _r(sum(r["ext_price"] for r in rows) * dvol)
    buy_lead = max([(_select_vendor(r.get("vendors") or []) or {}).get("lead_days", 3) for r in rows] or [3])

    built_for = str(asm.meta.get("build_config") or "")

    def entries_for(cfg, q):
        es = [_estimate(g, cfg, q, active, built_for) for g in groups]
        return es + [dict(e) for e in _elec_estimates(design, asm, efinal, q)]

    configs, per_cfg = {}, {}
    for cfg in CONFIGS:
        es = entries_for(cfg, nq)
        per_cfg[cfg] = es
        fab, tooling, setup, unit = _config_cost(es, buy_total, nq)
        fab_lead = max([e["lead_days"] for e in es] or [0])
        curve = []
        for q in (1, 10, 100, 1000, 10000):
            eq = es if q == nq else entries_for(cfg, q)
            bq = _r(sum(r["ext_price"] for r in rows) * _volume_discount(q))
            curve.append({"qty": q, "unit_cost": _config_cost(eq, bq, q)[3]})
        procs = Counter(e["process"] for e in es)
        configs[cfg] = {"unit_cost": unit, "tooling_cost": tooling, "lead_days": int(max(fab_lead, buy_lead) + 2),
                        "summary": _config_summary(cfg, es, nq, tooling), "parts_cost": fab, "purchased_cost": buy_total,
                        "setup_cost": setup, "fab_lead_days": int(fab_lead), "processes": dict(procs), "curve": curve}
    # break-even: production tooling vs the pilot process mix
    try:
        qs = np.unique(np.round(np.logspace(0, 4.3, 60)).astype(int))
        be = None
        for q in qs:
            pq = _config_cost(entries_for("pilot", int(q)), buy_total, int(q))[3]
            rq = _config_cost(entries_for("production", int(q)), buy_total, int(q))[3]
            if rq <= pq:
                be = int(q)
                break
        configs["production"]["breakeven_vs_pilot_qty"] = be
    except Exception:  # never block the plan on an insight
        be = None

    # ---------------------------------------------------------- active config: DFM + files
    es = per_cfg[active]
    gmap = {g.key: g for g in groups}
    for e in es:
        g = gmap.get(e["group"])
        try:
            if g is not None:
                e["dfm"] = dfm_checks(e, g.geo)
                e["files"] = write_files(e, g, outdir, design, efinal)
            else:
                e["files"] = _write_elec_files(e, outdir, efinal)
        except Exception as exc:  # one bad mesh must not sink the whole plan
            from ..services.durable import explain  # the page shows it: the projects folder by name (<rev>/mfg here)
            why = explain(exc, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(outdir)))))
            warnings.append(f"{e['group']}: machine files failed ({exc.__class__.__name__}: {why})")
        e["how"] = _how(e, design)
    # ---------------------------------------------------------- warnings / insights
    if nq < 5 and any(e["process"] == "pcb_assembly" for e in es):
        warnings.append(f"PCBA minimum order is 5 boards ({5 - nq} spare{'s' if 5 - nq != 1 else ''} at qty {nq})")
    if active == "production" and be and nq < be:
        warnings.append(f"production tooling (${configs['production']['tooling_cost']:,.0f}) only pays off above ~{be:,} units "
                        f"(pilot is cheaper at qty {nq:,})")
    if active != "prototype" and design.electronics.config == "modules" and efinal and (efinal.get("configs") or {}).get("custom_pcb"):
        c = efinal["configs"]
        delta = (c.get("modules", {}).get("cost") or 0) - (c["custom_pcb"].get("cost") or 0)
        if delta > 0:
            warnings.append(f"{active} build with off-the-shelf modules: the custom controller PCBA saves ~${delta:.0f}/unit "
                            "and most harness wires (electronics.config = custom_pcb)")
    for e in es:
        for d in e.get("dfm", []):
            if "exceeds the" in d:              # only what blocks manufacture; the rest are DFM warnings
                issues.append(f"{e['name']}: {d}")
    fab, tooling, setup, unit = _config_cost(es, buy_total, nq)
    out = {"config": active, "quantity": nq, "configs": configs, "parts": es,
           "totals": {"parts_cost": fab, "tooling_cost": tooling, "purchased_cost": buy_total, "setup_cost": setup,
                      "unit_cost": unit, "lead_days": configs[active]["lead_days"]},
           "cost_basis": COST_BASIS, "issues": issues, "warnings": warnings}
    _write_readme(out, outdir, design)
    out["readme"] = "README.txt"
    out["package"] = _write_zip(out, outdir, active)
    with open(os.path.join(outdir, "manufacturing.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=_json)
    return out


def _json(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    return str(o)


def _how(e, design) -> str:
    proc, files = e["process"], e.get("files", [])
    f = lambda ext: next((x.split("/")[-1] for x in files if x.endswith(ext)), None)
    if proc == "fdm_print":
        pr = e.get("print", {})
        lh = pr.get("layer_height_mm", 0.2)
        return (f"print on any FDM printer ({e['material']}, {lh:g} mm, 4 walls, 25 % gyroid"
                + (", tree supports" if pr.get("supports", "none") != "none" else "") + f") from {f('.3mf') or 'the 3MF'}"
                + (" or order from JLC3DP" if e["vendor"].startswith("In-house") else f"; order from {e['vendor']}"))
    if proc == "sla_print":
        return f"upload {f('.stl') or 'the STL'} to {e['vendor']} (tough resin) or print on a Formlabs-class SLA"
    if proc == "mjf_print":
        return f"upload {f('.stl') or 'the STL'} to {e['vendor']} (HP MJF PA12, dyed black)"
    if proc == "cnc_mill":
        return f"order CNC from {e['vendor']} with {f('.dxf') or 'the DXF'}" + (" + STEP" if e["class"] != "plate" else "") + \
               f" ({e['material']}); {f('.nc') or 'G-code'} for an in-house mill"
    if proc == "cnc_turn":
        return f"order turning from {e['vendor']} ({e['material']}), drawing from {f('.dxf') or 'the DXF'}"
    if proc == "laser_cut":
        return f"upload {f('.dxf') or 'the DXF'} to {e['vendor']} ({e['material']})"
    if proc == "sheet_metal":
        return f"upload {f('_flat.dxf') or 'the flat DXF'} + bend table to {e['vendor']} ({e['material']}, laser + bend)"
    if proc == "injection_mold":
        return f"request a mold quote from {e['vendor']} with the STEP (notes in {f('_molding.txt') or 'molding notes'})"
    if proc == "pcb_assembly":
        return "order PCBA from JLCPCB with gerbers.zip + jlc_bom.csv + jlc_cpl.csv"
    if proc == "cable_assembly":
        return "crimp from harness_cutlist.csv (JST/Molex crimp tool)" if e["vendor"].startswith("In-house") else \
               f"order the harness from {e['vendor']} with harness_cutlist.csv"
    return "fabricate per drawing"


def _write_readme(m, outdir, design):
    L = [f"MORPH MANUFACTURING PACKAGE — {design.name} — build config: {m['config']} × {m['quantity']}", "=" * 72,
         COST_BASIS, "",
         f"Unit cost ${m['totals']['unit_cost']:,.2f}  (fabricated ${m['totals']['parts_cost']:,.2f} + purchased "
         f"${m['totals']['purchased_cost']:,.2f} + tooling ${m['totals']['tooling_cost']:,.0f} / {m['quantity']})",
         f"Lead time ≈ {m['totals']['lead_days']} days", "", "WHAT TO SEND WHERE", "-" * 72]
    by_v = defaultdict(list)
    for e in m["parts"]:
        by_v[e["vendor"]].append(e)
    for v, es in by_v.items():
        url = next((q["url"] for q in es[0]["quotes"] if q["vendor"] == v and q["url"]), "")
        L.append(f"{v}  {url}")
        for e in es:
            L.append(f"   {e['qty']:>3} × {e['name']:<36} {PROC_LABEL.get(e['process'], e['process']):<16} "
                     f"${e['unit_cost']:>8.2f}  {', '.join(x.split('/')[-1] for x in e.get('files', []))}")
        L.append("")
    L += ["BUILD CONFIG COMPARISON", "-" * 72]
    for k, c in m["configs"].items():
        L.append(f"{k:<11} ${c['unit_cost']:>10,.2f}/unit  tooling ${c['tooling_cost']:>9,.0f}  lead {c['lead_days']:>3} d   {c['summary']}")
    if m["warnings"]:
        L += ["", "WARNINGS"] + [f" - {w}" for w in m["warnings"]]
    with open(os.path.join(outdir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def _write_zip(m, outdir, cfg):
    name = f"mfg_{cfg}.zip"
    with zipfile.ZipFile(os.path.join(outdir, name), "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        z.write(os.path.join(outdir, "README.txt"), "README.txt")
        for e in m["parts"]:
            for f in e.get("files", []):
                p = os.path.join(outdir, f)
                if os.path.isfile(p):       # 3MF / zip are already compressed: store them
                    z.write(p, f, compress_type=zipfile.ZIP_STORED if f.endswith((".3mf", ".zip")) else zipfile.ZIP_DEFLATED)
    return name


# ================================================================== BOM
def bom(design, asm, mfg, efinal=None) -> dict:
    """Merged BOM: purchased rows (catalog + electronics sourcing + fasteners + cables) and fabricated rows
    from the manufacturing plan (+ amortized tooling). Totals match mfg['configs'][active]['unit_cost']."""
    mfg = mfg or {}
    nq = max(1, int(mfg.get("quantity") or design.build.quantity or 1))
    elec_entries = [e for e in mfg.get("parts", []) if e.get("process") in ("pcb_assembly", "cable_assembly")]
    rows = buy_rows(design, asm, efinal, elec_entries)
    dvol = _volume_discount(nq)
    if dvol < 1.0:
        for r in rows:
            if r.get("selected"):
                r["selected"]["unit_price"] = _r(r["selected"]["unit_price"] * dvol)
                r["ext_price"] = _r(r["selected"]["unit_price"] * r["qty"])
                r["note"] = f"volume price at {nq} units (-{(1 - dvol) * 100:.0f} %)"
    buy_sum = sum(r["ext_price"] for r in rows)
    target_buy = (mfg.get("totals") or {}).get("purchased_cost")
    if target_buy is not None and abs(buy_sum - target_buy) > 0.005 and rows:
        rows[-1]["ext_price"] = _r(rows[-1]["ext_price"] + target_buy - buy_sum)     # rounding residue only
    for e in mfg.get("parts", []):
        if not e.get("in_bom", True):
            continue
        cat = {"pcb_assembly": "pcb", "cable_assembly": "cable"}.get(e["process"], "fabricated")
        vendors = [{k: q[k] for k in ("vendor", "url", "unit_price", "lead_days", "note")} for q in e.get("quotes", [])]
        rows.append({"item": e["name"], "category": cat, "qty": e["qty"], "make_or_buy": "make",
                     "how": e.get("how") or _how(e, design), "vendors": vendors,
                     "selected": {"vendor": e["vendor"], "unit_price": e["unit_cost"]},
                     "ext_price": _r(e["unit_cost"] * e["qty"]), "group": e["group"], "process": e["process"],
                     "material": e.get("material", ""), "files": e.get("files", [])})
    tooling = (mfg.get("totals") or {}).get("tooling_cost") or 0.0
    if tooling:
        n = sum(1 for e in mfg.get("parts", []) if e.get("tooling_cost"))
        rows.append({"item": f"Injection-mold tooling ({n} tool{'s' if n != 1 else ''}, ${tooling:,.0f} amortized over {nq:,} units)",
                     "category": "fabricated", "qty": 1, "make_or_buy": "make",
                     "how": "one-time NRE: aluminium/steel molds, paid with the first order", "vendors": [],
                     "selected": None, "ext_price": _r(tooling / nq), "process": "injection_mold"})
    buy = _r(sum(r["ext_price"] for r in rows if r["make_or_buy"] == "buy"))
    make = _r(sum(r["ext_price"] for r in rows if r["make_or_buy"] == "make"))
    per = {k: v.get("unit_cost") for k, v in (mfg.get("configs") or {}).items()}
    mats = material_rows(mfg, efinal, nq)
    return {"rows": rows + mats, "totals": {"buy": buy, "make": make, "total": _r(buy + make), "per_config": per,
                                            "tooling": _r(tooling), "lines": len(rows), "pieces": int(sum(r["qty"] for r in rows)),
                                            "materials": len(mats), "quantity": nq, "config": mfg.get("config", design.build.config)},
            "cost_basis": COST_BASIS}


# -- bulk materials ----------------------------------------------------------------------------
# Stock you buy by the spool / bottle / plate to make the parts yourself. Links only (search and
# category pages that resolved on 2026-10-11); nothing is ordered. These rows are NOT added to the
# totals: each fabricated part's price already includes its material.

def _amazon(q: str) -> str:
    return "https://www.amazon.com/s?k=" + q.replace(" ", "+")


def _mlink(vendor, url, price, lead, note=""):
    return {"vendor": vendor, "url": url, "unit_price": _r(price), "lead_days": lead, "note": note}


FILAMENT_LINKS = {
    "PETG": [("prusa", "https://www.prusa3d.com/category/prusament-petg/"), ("polymaker", "https://us.polymaker.com/collections/all")],
    "PLA": [("prusa", "https://www.prusa3d.com/category/prusament-pla/"), ("polymaker", "https://us.polymaker.com/collections/all")],
}
STOCK_LINKS = {
    "Al6061": "https://www.mcmaster.com/6061-aluminum/", "Al7075": "https://www.mcmaster.com/products/aluminum/",
    "Al5052": "https://www.mcmaster.com/5052-aluminum/", "Delrin": "https://www.mcmaster.com/delrin/",
    "POM": "https://www.mcmaster.com/acetal/",
}
MAT_NAME = {"Al6061": "6061-T6 aluminium", "Al7075": "7075 aluminium", "Al5052": "5052-H32 aluminium", "Delrin": "Delrin (acetal)",
            "POM": "acetal (POM)", "Steel": "mild steel", "Stainless": "stainless steel", "Brass": "brass"}


def material_rows(mfg: dict, efinal: dict | None = None, nq: int = 1) -> list:
    """Bulk materials for making the robot's parts in-house: filament, resin, plate/bar stock, sheet, wire."""
    parts = [e for e in (mfg or {}).get("parts", []) if e.get("in_bom", True)]
    out = []

    def add(item, qty, unit, unit_price, how, vendors, need):
        out.append({"item": item, "category": "material", "qty": qty, "make_or_buy": "buy", "how": how, "vendors": vendors,
                    "selected": {"vendor": vendors[0]["vendor"], "unit_price": vendors[0]["unit_price"]} if vendors else None,
                    "ext_price": _r(qty * unit_price), "in_total": False, "unit": unit, "need": need,
                    "note": "bulk stock for making the parts yourself; not added to the total (each part's price includes its material)"})

    # FDM filament, per material: grams of every FDM part (+10 % for supports, purge and failed prints)
    grams = {}
    for e in parts:
        if e.get("process") == "fdm_print":
            g = ((e.get("cost_model") or {}).get("inputs") or {}).get("grams") or 0.0
            grams[e.get("material") or "PETG"] = grams.get(e.get("material") or "PETG", 0.0) + g * e["qty"] * nq
    for m, g in sorted(grams.items(), key=lambda kv: -kv[1]):
        spool = 500.0 if m == "TPU" else 1000.0
        per_kg = MAT.get(m, (1.2, 25.0))[1]
        price = per_kg * spool / 1000.0
        n = max(1, math.ceil(g * 1.1 / spool))
        q = f"{m} filament 1.75mm {'95A ' if m == 'TPU' else ''}{int(spool)}g"
        vend = [_mlink("Amazon", _amazon(q), price, 2)]
        for name, url in FILAMENT_LINKS.get(m, []):
            vend.append(_mlink({"prusa": "Prusa (Prusament)", "polymaker": "Polymaker"}[name], url, price * 1.25, 5))
        amount = f"{g / 1000:.2f} kg" if g >= 100 else f"{g:.0f} g"
        add(f"{m} filament, 1.75 mm, {int(spool)} g spool", n, "spool", price,
            f"{amount} for every FDM part (+10 % supports and purge); print services include their own material",
            vend, amount)

    # SLA resin
    ml = sum((((e.get("cost_model") or {}).get("inputs") or {}).get("resin_cm3") or 0.0) * e["qty"] * nq
             for e in parts if e.get("process") == "sla_print")
    if ml:
        n = max(1, math.ceil(ml * 1.15 / 1000.0))
        add("Tough engineering resin, 1 L", n, "bottle", 149.0,
            f"{ml:.0f} mL for every SLA part (+15 % supports and tank)",
            [_mlink("Formlabs", "https://formlabs.com/store/materials/", 149.0, 4, "Tough 2000 class"),
             _mlink("Elegoo", "https://us.elegoo.com/collections/resin", 45.0, 4, "ABS-like resin"),
             _mlink("Amazon", _amazon("tough resin 1L 405nm"), 40.0, 2)], f"{ml:.0f} mL")

    # CNC stock (plate and bar), per material: the stock volume the cost model assumed
    stock = {}
    for e in parts:
        if e.get("process") in ("cnc_mill", "cnc_turn"):
            v = ((e.get("cost_model") or {}).get("inputs") or {}).get("stock_cm3") or 0.0
            stock[e.get("material") or "Al6061"] = stock.get(e.get("material") or "Al6061", 0.0) + v * e["qty"] * nq
    for m, v in sorted(stock.items(), key=lambda kv: -kv[1]):
        dens, per_kg = MAT.get(m, (2.7, 9.0))
        kg = v * dens / 1000.0 * 1.25                     # +25 % saw kerf, squaring and offcuts
        nm = MAT_NAME.get(m, m)
        vend = [_mlink("McMaster-Carr", STOCK_LINKS.get(m, "https://www.mcmaster.com/products/aluminum/"), per_kg * 2.5, 2, "cut to size"),
                _mlink("Amazon", _amazon(f"{nm} plate"), per_kg * 1.8, 3)]
        add(f"{nm} plate / bar stock", _r(kg, 2), "kg", per_kg * 2.5,
            f"{v:.0f} cm\u00b3 of stock for the CNC parts (+25 % kerf and offcuts); machine shops supply their own",
            vend, f"{kg:.2f} kg")

    # sheet stock, per material and gauge
    sheet = {}
    for e in parts:
        if e.get("process") in ("sheet_metal", "laser_cut"):
            inp = (e.get("cost_model") or {}).get("inputs") or {}
            k = (e.get("material") or "Al5052", inp.get("thickness_mm"))
            sheet[k] = sheet.get(k, 0.0) + (inp.get("sheet_cm2") or 0.0) * e["qty"] * nq
    for (m, t), cm2 in sorted(sheet.items(), key=lambda kv: -kv[1]):
        if not cm2:
            continue
        nm = MAT_NAME.get(m, m)
        side = math.ceil(math.sqrt(cm2 * 1.3) / 5.0) * 5          # square blank, +30 % nesting margin, 5 cm steps
        price = 0.012 * side * side * (t or 1.5) / 1.5
        add(f"{nm} sheet, {t or '?'} mm, {side} x {side} cm", 1, "sheet", price,
            f"{cm2:.0f} cm\u00b2 of flat patterns (+30 % nesting); or order the cut, bent parts",
            [_mlink("McMaster-Carr", STOCK_LINKS.get(m, "https://www.mcmaster.com/products/aluminum/"), price, 2),
             _mlink("SendCutSend", "https://sendcutsend.com/materials/", price, 5, "cut and bent from your DXF"),
             _mlink("Amazon", _amazon(f"{nm} sheet {t} mm"), price * 0.9, 3)], f"{cm2:.0f} cm\u00b2")

    # hook-up wire for the harness, per gauge
    wire = {}
    for w in (efinal or {}).get("harness") or []:
        hit = re.search(r"(\d+)\s*AWG", str(w.get("wire") or ""))
        if hit and w.get("length_mm"):
            awg = int(hit.group(1))
            wire[awg] = wire.get(awg, 0.0) + w["length_mm"] * (w.get("conductors") or 2) * nq / 1000.0
    wire_links = lambda q, p: [_mlink("Amazon", _amazon(q), p, 2, "6 colours"),  # noqa: E731
                               _mlink("Pololu", "https://www.pololu.com/category/215/wires", p * 1.3, 4),
                               _mlink("McMaster-Carr", "https://www.mcmaster.com/silicone-wire/", p * 1.6, 2)]
    short = {a: m for a, m in wire.items() if m < 2.0}
    for awg, m in sorted(wire.items()):
        if awg in short:
            continue
        add(f"{awg} AWG silicone hook-up wire, 10 m pack", max(1, math.ceil(m * 1.3 / 10.0)), "pack", 9.0,
            f"{m:.1f} m of conductor across the harness (+30 % for service loops and mistakes)",
            wire_links(f"{awg} AWG silicone wire", 9.0), f"{m:.1f} m")
    if short:
        gauges = ", ".join(str(a) for a in sorted(short))
        add(f"Silicone hook-up wire kit, 18-30 AWG (for the short runs: {gauges} AWG)", 1, "kit", 16.0,
            f"{sum(short.values()):.1f} m in short power and sensor runs; one assorted kit covers them",
            wire_links("silicone wire kit 18 20 22 24 26 28 AWG", 16.0), f"{sum(short.values()):.1f} m")
    return out
