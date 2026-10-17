"""KiCad 6 to 9 boards and schematics -> BOM lines and the board outline (for the controller plate and Darwin).

  res = read("carrier.kicad_pcb")
  res -> {"document": "pcb", "version": 20240108, "board_mm": [92.0, 66.0], "footprints": 61, "layers": 2,
          "rows": [{"item": "10k 0603", "qty": 4, "refs": "R1, R2, R3, R4", "footprint": ..., "make_or_buy": "buy"}, ...]}

A ~40-line S-expression reader with depth and token limits; nothing is evaluated. Footprints or symbols marked
exclude_from_bom, board_only or dnp are left out; power symbols (#PWR, #FLG) too. KiCad 5 files are refused with
a "save it in KiCad 6 or newer" message (detect.py).
"""

from __future__ import annotations

import math
import os
import re

from .common import MB, ImportFailed

TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def sexpr(text: str, max_depth: int = 400, max_tokens: int = 3_000_000):
    stack, cur, n = [], [], 0
    pos, end = 0, len(text)
    while pos < end:
        m = TOK.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip():
                raise ImportFailed(f"Unreadable KiCad file near character {pos}.")
            break
        pos = m.end()
        o, c, s, a = m.groups()
        if o:
            stack.append(cur)
            cur = []
            if len(stack) > max_depth:
                raise ImportFailed("The KiCad file is nested too deeply.")
        elif c:
            if not stack:
                raise ImportFailed("The KiCad file has unbalanced parentheses.")
            node, cur = cur, stack.pop()
            cur.append(node)
        elif s is not None or a is not None:
            cur.append(s.replace('\\"', '"') if s is not None else a)
        n += 1
        if n > max_tokens:
            raise ImportFailed("The KiCad file is too large to read.")
    if stack or not cur or not isinstance(cur[0], list):
        raise ImportFailed("The KiCad file is incomplete (unbalanced parentheses).")
    return cur[0]


def find(node, key) -> list:
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def first(node, key):
    f = find(node, key)
    return f[0] if f else None


def prop(node, name):
    for x in find(node, "property"):
        if len(x) > 2 and x[1] == name:
            return x[2]
    for x in find(node, "fp_text"):                      # KiCad 6 footprints
        if len(x) > 2 and x[1] == name.lower():
            return x[2]
    return None


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _outline(pcb) -> list | None:
    pts = []

    def take(e):
        for k in ("start", "end", "mid", "center"):
            for s in find(e, k):
                if len(s) >= 3:
                    pts.append((_num(s[1]), _num(s[2])))
        for p in find(e, "pts"):
            pts.extend((_num(xy[1]), _num(xy[2])) for xy in find(p, "xy") if len(xy) >= 3)
        if e[0] in ("gr_circle", "fp_circle"):
            c, en = first(e, "center"), first(e, "end")
            if c and en:
                r = math.hypot(_num(en[1]) - _num(c[1]), _num(en[2]) - _num(c[2]))
                pts.extend([(_num(c[1]) - r, _num(c[2]) - r), (_num(c[1]) + r, _num(c[2]) + r)])

    def on_edge(e):
        return any(len(l) > 1 and l[1] == "Edge.Cuts" for l in find(e, "layer"))

    for e in pcb:
        if isinstance(e, list) and e and e[0] in ("gr_line", "gr_rect", "gr_poly", "gr_arc", "gr_circle", "gr_curve") and on_edge(e):
            take(e)
    if not pts:
        return None
    xs, ys = zip(*pts)
    return [round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)]


def _attrs(node) -> set:
    a = first(node, "attr")
    return set(a[1:]) if a else set()


def _rows(items: list) -> list:
    groups = {}
    for it in items:
        key = (it["value"], it["footprint"], it.get("mpn") or "")
        g = groups.setdefault(key, {**it, "refs": [], "qty": 0})
        g["refs"].append(it["ref"])
        g["qty"] += 1
    out = []
    for (value, fp, mpn), g in sorted(groups.items(), key=lambda kv: kv[1]["refs"][0]):
        short_fp = fp.split(":")[-1] if fp else ""
        refs = sorted(g["refs"], key=lambda r: (re.sub(r"\d+", "", r), int(re.sub(r"\D", "", r) or 0)))
        out.append({"item": f"{value} {short_fp}".strip()[:200], "qty": g["qty"], "refs": ", ".join(refs)[:400],
                    "value": value, "footprint": fp, "mpn": mpn or None, "manufacturer": g.get("manufacturer"),
                    "unit_price": None, "ext_price": None, "url": None, "make_or_buy": "buy",
                    "category": "electronics", "source": "kicad"})
    return out


def read(path: str) -> dict:
    if os.path.getsize(path) > 50 * MB:
        raise ImportFailed(f"{os.path.basename(path)} is over 50 MB.")
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    head = text.lstrip()[:20]
    if head.startswith("EESchema") or head.startswith("PCBNEW"):
        raise ImportFailed(f"{os.path.basename(path)} is a KiCad 5 or older file; open it in KiCad 6 or newer and save it again.")
    root = sexpr(text)
    kind = root[0] if root and isinstance(root[0], str) else ""
    ver = first(root, "version")
    version = int(_num(ver[1])) if ver else None
    warnings = []
    if version is not None and version < 20211014:
        warnings.append(f"file version {version} predates KiCad 6; some fields may be missing")
    if kind == "kicad_pcb":
        items = []
        fps = find(root, "footprint") or find(root, "module")
        for fp in fps:
            at = _attrs(fp)
            ref = prop(fp, "Reference") or "?"
            if {"exclude_from_bom", "board_only", "dnp"} & at or ref.startswith("#"):
                continue
            items.append({"ref": ref, "value": prop(fp, "Value") or "", "footprint": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else "",
                          "mpn": prop(fp, "MPN") or prop(fp, "LCSC") or prop(fp, "Mfr. Part #"),
                          "manufacturer": prop(fp, "Manufacturer")})
        layers = sum(1 for l in (first(root, "layers") or [])[1:] if isinstance(l, list) and len(l) > 2 and l[2] in ("signal", "power", "mixed"))
        outline = _outline(root)
        if outline is None:
            warnings.append("no board outline on Edge.Cuts")
        return {"document": "pcb", "version": version, "board_mm": outline, "footprints": len(fps), "layers": layers,
                "rows": _rows(items), "warnings": warnings}
    if kind == "kicad_sch":
        items = []
        syms = [s for s in find(root, "symbol") if first(s, "lib_id")]
        for s in syms:
            lib = first(s, "lib_id")[1] if len(first(s, "lib_id")) > 1 else ""
            ref = prop(s, "Reference") or "?"
            inb, dnp = first(s, "in_bom"), first(s, "dnp")
            if lib.startswith("power:") or ref.startswith("#") or (inb and len(inb) > 1 and inb[1] == "no") or \
                    (dnp and len(dnp) > 1 and dnp[1] == "yes"):
                continue
            items.append({"ref": ref, "value": prop(s, "Value") or "", "footprint": prop(s, "Footprint") or "",
                          "mpn": prop(s, "MPN") or prop(s, "LCSC"), "manufacturer": prop(s, "Manufacturer")})
        sheets = len(find(root, "sheet"))
        if sheets:
            warnings.append(f"{sheets} hierarchical sheets are not read; import the .kicad_pcb for the full parts list")
        return {"document": "schematic", "version": version, "board_mm": None, "footprints": len(items), "layers": None,
                "rows": _rows(items), "warnings": warnings}
    raise ImportFailed(f"{os.path.basename(path)} is not a KiCad board or schematic.")
