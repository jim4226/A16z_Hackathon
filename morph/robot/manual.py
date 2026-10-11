"""Assembly, wiring and bring-up manual for a Morph robot.

    man = build(design, asm, mfg, efinal, outdir)

Writes `outdir/manual.json` and a standalone, printable `outdir/manual.html` (images embedded, no network):
cover page with the robot's spec table, contents, kit (fabricated + purchased parts and fasteners with
counts and thumbnails), tools and safety, one page per assembly step (illustration with the new parts
highlighted and earlier ones ghosted, parts table, fastener + torque callouts, numbered instructions,
warnings, time), then wiring (from efinal['harness']), first power-on safety checks, firmware flashing +
servo IDs, calibration (joints, gripper, force sensors) and the first pick test, plus a torque table,
fastener totals and a sign-off sheet.

Illustrations come from a small software renderer (painter's algorithm into a face-ID buffer, then
per-pixel shading and outline extraction in numpy): line-art in the style of a product manual, a few
hundred ms per image, no browser needed.

Returns {"json","html","steps":[{index,title,parts,fasteners,tools,instructions,warnings,time_min,...}],
"tools","time_min_total"}. Mechanical steps keep the assembly's step indices (the Studio viewer highlights
parts by `Part.step`); wiring / bring-up steps follow with the next indices.
"""

from __future__ import annotations

import base64
import datetime
import html
import io
import json
import math
import os
import re
from collections import Counter, OrderedDict

import numpy as np
import trimesh

from . import catalog
from . import manufacturing as mf

# ================================================================== renderer
_INK = np.array([0.11, 0.14, 0.18])
SS_STEP = 1.5          # supersampling of the large step illustrations (thumbnails and the cover use 2)
_GHOST_INK = np.array([0.66, 0.70, 0.75])


def _hex(c):
    c = str(c).lstrip("#")
    if len(c) != 6:
        return np.array([0.75, 0.77, 0.8])
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], float) / 255.0


def _camera(az_deg, el_deg):
    az, el = math.radians(az_deg), math.radians(el_deg)
    d = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
    up = np.array([0.0, 0.0, 1.0])
    r = np.cross(up, d)
    r /= np.linalg.norm(r)
    u = np.cross(d, r)
    return d, r, u


def _cluster(mesh, cell):
    """Vertex-clustering decimation (drops thread / knurl detail far below a pixel)."""
    if cell <= 0 or len(mesh.faces) < 400:
        return np.asarray(mesh.vertices), np.asarray(mesh.faces)
    V = np.asarray(mesh.vertices)
    q = np.floor(V / cell).astype(np.int64)
    q -= q.min(axis=0)
    key = (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]          # one int64 per cell: 1-D unique is fast
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    v = np.zeros((cnt.size, 3))
    np.add.at(v, inv, V)
    v /= cnt[:, None]
    f = inv[np.asarray(mesh.faces)]
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    return v, f[keep]


def _subdivide(v, f, max_edge):
    e = np.linalg.norm(v[f[:, [1, 2, 0]]] - v[f], axis=2).max(axis=1)
    if e.max() <= max_edge:
        return v, f
    try:
        return trimesh.remesh.subdivide_to_size(v, f, max_edge=max_edge, max_iter=6)
    except Exception:
        return v, f


def render(items, view=(38.0, 24.0), size=(900, 620), ss=2, focus_frame=None, bg=(255, 255, 255), pad=0.07,
           shadow=True, arrows=(), cache=None, lines=(), dots=()):
    """items: [(mesh_in_world_mm, '#rrggbb', style)] with style in normal | focus | ghost.
    focus_frame: optional (N, 3) world points to frame (zoom); default frames everything.
    arrows: [(p_from, p_to)] world points, drawn as dashed motion arrows; lines: [(points (N, 3), '#rrggbb', width_px)]
    overlay polylines (cable routes); dots: [(point, '#rrggbb', radius_px, label)]. Returns a PIL RGB image."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = int(round(size[0] * ss)), int(round(size[1] * ss))
    d, r, u = _camera(*view)
    P = np.stack([r, u, d], axis=1)               # world -> (x_screen, y_screen, depth)
    allv = np.vstack([m.vertices for m, _, _ in items if len(m.vertices)]) if items else np.zeros((1, 3))
    sv = allv @ P
    frame = np.asarray(focus_frame, float) @ P if focus_frame is not None and len(focus_frame) else sv
    lo, hi = frame[:, :2].min(0), frame[:, :2].max(0)
    span = np.maximum(hi - lo, 1e-6)
    scale = float(min(W * (1 - 2 * pad) / span[0], H * (1 - 2 * pad) / span[1]))
    cx, cy = (lo + hi) / 2
    px_mm = 1.0 / scale
    tris, cols, styles, pids, normals = [], [], [], [], []
    scene = float(np.linalg.norm(sv[:, :2].max(0) - sv[:, :2].min(0)))
    for k, (m, color, style) in enumerate(items):
        if len(m.faces) == 0:
            continue
        cell = 2.0 ** round(math.log2(max(px_mm * (2.8 if style == "ghost" else 1.4), 1e-3)))   # quantized for the cache
        ck = (id(m), cell)
        if cache is not None and ck in cache:
            v, f = cache[ck]
        else:
            v, f = _cluster(m, cell)
            if len(f):
                v, f = _subdivide(v, f, max(scene / 14.0, cell * 8))
            if cache is not None:
                cache[ck] = (v, f)
        if len(f) == 0:
            continue
        t = v[f]
        n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        ln = np.linalg.norm(n, axis=1)
        ok = ln > 1e-12
        t, n = t[ok], n[ok] / ln[ok, None]
        front = n @ d > -0.02
        t, n = t[front], n[front]
        tris.append(t)
        normals.append(n)
        cols.append(np.repeat(_hex(color)[None], len(t), 0))
        styles.append(np.full(len(t), {"ghost": 0, "normal": 1, "focus": 2}.get(style, 1), np.int8))
        pids.append(np.full(len(t), k, np.int32))
    img = Image.new("RGB", (W, H), bg)
    if not tris:
        return img.resize(size, Image.LANCZOS)
    T = np.concatenate(tris)
    N = np.concatenate(normals)
    C = np.concatenate(cols)
    S = np.concatenate(styles)
    PI = np.concatenate(pids)
    st = T @ P                                    # (F, 3, 3) screen coords + depth
    xs = (st[:, :, 0] - cx) * scale + W / 2
    ys = H / 2 - (st[:, :, 1] - cy) * scale
    depth = st[:, :, 2].mean(1)
    inside = (xs.max(1) >= 0) & (xs.min(1) < W) & (ys.max(1) >= 0) & (ys.min(1) < H)
    area2 = np.abs((xs[:, 1] - xs[:, 0]) * (ys[:, 2] - ys[:, 0]) - (xs[:, 2] - xs[:, 0]) * (ys[:, 1] - ys[:, 0]))
    inside &= area2 > 0.15                         # sub-pixel slivers add nothing at this scale
    order = np.argsort(depth[inside])
    sel = np.nonzero(inside)[0][order]
    idimg = Image.new("I", (W, H), 0)
    core = ImageDraw.Draw(idimg).draw
    flat = np.stack([xs[sel, 0], ys[sel, 0], xs[sel, 1], ys[sel, 1], xs[sel, 2], ys[sel, 2]], axis=1).tolist()
    ids = (sel + 1).tolist()                       # 32-bit face-ID buffer: 0 = background
    dp = core.draw_polygon
    for xy, ink in zip(flat, ids):
        dp(xy, ink, 1)
    fid = np.asarray(idimg, np.int32) - 1
    mask = fid >= 0
    # flat shading is per face: shade the faces, then gather once
    L1 = 0.40 * r + 0.75 * u + 0.52 * d
    L1 /= np.linalg.norm(L1)
    L2 = -0.55 * r + 0.15 * u + 0.80 * d
    L2 /= np.linalg.norm(L2)
    Hh = L1 + d
    Hh /= np.linalg.norm(Hh)
    k1 = np.clip(N @ L1, 0, 1)[:, None]
    k2 = np.clip(N @ L2, 0, 1)[:, None]
    spec = np.clip(N @ Hh, 0, 1)[:, None] ** 28
    FC = np.clip(C * (0.46 + 0.46 * k1 + 0.16 * k2) + 0.10 * spec, 0, 1)
    gh = S == 0
    FC[gh] = (0.80 + 0.13 * np.clip(0.4 + 0.6 * k1[gh], 0, 1)) * np.array([0.985, 0.99, 1.0])
    bgc = np.array(bg, float) / 255.0
    FC = FC.astype(np.float32)
    out = np.empty((H, W, 3), np.float32)
    out[:] = bgc
    fm = fid[mask]
    out[mask] = FC[fm]
    part = np.full((H, W), -1, np.int32)
    part[mask] = PI[fm]
    sty = np.full((H, W), -1, np.int8)
    sty[mask] = S[fm]
    # outlines: part boundaries + silhouettes (4-neighbourhood), creases where the face normal turns > 35 deg
    hb = part[:, :-1] != part[:, 1:]
    vb = part[:-1, :] != part[1:, :]
    edge = np.zeros((H, W), bool)
    edge[:, :-1] |= hb
    edge[:, 1:] |= hb
    edge[:-1, :] |= vb
    edge[1:, :] |= vb
    crease = np.zeros((H, W), bool)
    for A_, B_, sl_a, sl_b in ((fid[:, :-1], fid[:, 1:], (slice(None), slice(0, -1)), (slice(None), slice(1, None))),
                               (fid[:-1, :], fid[1:, :], (slice(0, -1), slice(None)), (slice(1, None), slice(None)))):
        cand = (A_ != B_) & (A_ >= 0) & (B_ >= 0)
        same_part = np.zeros_like(cand)
        pa = part[sl_a]
        pb = part[sl_b]
        same_part[cand] = pa[cand] == pb[cand]
        cand &= same_part
        iy, ix = np.nonzero(cand)
        if len(iy):
            ndot = np.einsum("ij,ij->i", N[A_[iy, ix]], N[B_[iy, ix]])
            hit = ndot < 0.82
            crease[sl_a][iy[hit], ix[hit]] = True
    sty_n = sty.copy()
    sty_n[:-1, :] = np.maximum(sty_n[:-1, :], sty[1:, :])
    thick = edge.copy()
    thick[1:, :] |= edge[:-1, :]
    thick[:, 1:] |= edge[:, :-1]
    dark = thick & (sty_n != 0)
    out[dark] = _INK
    light = edge & (sty_n == 0)
    out[light] = _GHOST_INK
    cm = crease & ~thick
    gcm = cm & (sty == 0)
    out[gcm] = 0.5 * _GHOST_INK + 0.5 * out[gcm]
    ncm = cm & (sty != 0)
    out[ncm] = 0.55 * _INK + 0.45 * out[ncm]
    np.clip(out, 0, 1, out=out)
    out *= 255.0
    res = Image.fromarray(out.astype(np.uint8), "RGB")
    if shadow:
        base_pts = allv[allv[:, 2] < allv[:, 2].min() + 3.0]
        if len(base_pts) > 3:
            c0 = base_pts[:, :2].mean(0)
            rad = float(np.percentile(np.linalg.norm(base_pts[:, :2] - c0, axis=1), 95)) * 1.25
            ang = np.linspace(0, 2 * math.pi, 64)
            ring = np.stack([c0[0] + rad * np.cos(ang), c0[1] + rad * np.sin(ang), np.full(64, allv[:, 2].min())], 1) @ P
            sh = Image.new("L", (W, H), 0)
            ImageDraw.Draw(sh).polygon([((x - cx) * scale + W / 2, H / 2 - (y - cy) * scale) for x, y, _ in ring], fill=70)
            sh = sh.filter(ImageFilter.GaussianBlur(radius=float(max(6.0, 0.06 * rad * scale))))
            sa = np.asarray(sh, np.float32) / 255.0
            free = ~(mask | thick)
            o = np.asarray(res, np.float32)
            o[free] *= (1 - 0.35 * sa[free])[:, None]
            res = Image.fromarray(o.astype(np.uint8), "RGB")
    def scr(p):
        a_ = np.asarray(p, float) @ P
        return (a_[0] - cx) * scale + W / 2, H / 2 - (a_[1] - cy) * scale

    if lines or dots:
        dr3 = ImageDraw.Draw(res)
        for pts3, color, width in lines:
            xy = [scr(p) for p in pts3]
            rgb = tuple(int(round(c * 255)) for c in _hex(color))
            if len(xy) > 1:
                dr3.line(xy, fill=(255, 255, 255), width=int((width + 3) * ss), joint="curve")
                dr3.line(xy, fill=rgb, width=int(width * ss), joint="curve")
            for x, y in (xy[0], xy[-1]):
                rr = (width + 2) * ss
                dr3.ellipse([x - rr, y - rr, x + rr, y + rr], fill=rgb, outline=(255, 255, 255), width=max(1, int(ss)))
        for p3, color, rad, label in dots:
            x, y = scr(p3)
            rgb = tuple(int(round(c * 255)) for c in _hex(color))
            rr = rad * ss
            dr3.ellipse([x - rr, y - rr * 0.55, x + rr, y + rr * 0.55], outline=rgb, width=int(3 * ss))
            dr3.ellipse([x - 3 * ss, y - 3 * ss, x + 3 * ss, y + 3 * ss], fill=rgb)
    if arrows:
        dr2 = ImageDraw.Draw(res)
        for p0, p1 in arrows:
            a0 = np.asarray(p0) @ P
            a1 = np.asarray(p1) @ P
            x0, y0 = (a0[0] - cx) * scale + W / 2, H / 2 - (a0[1] - cy) * scale
            x1, y1 = (a1[0] - cx) * scale + W / 2, H / 2 - (a1[1] - cy) * scale
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 12 * ss:
                continue
            ux, uy = (x1 - x0) / L, (y1 - y0) / L
            dash = 9 * ss
            s = 0.0
            while s < L - 14 * ss:
                e = min(s + dash, L - 14 * ss)
                dr2.line([(x0 + ux * s, y0 + uy * s), (x0 + ux * e, y0 + uy * e)], fill=(255, 106, 26), width=int(2 * ss))
                s += dash * 1.7
            hx, hy = x1, y1
            dr2.polygon([(hx, hy), (hx - ux * 14 * ss - uy * 6 * ss, hy - uy * 14 * ss + ux * 6 * ss),
                         (hx - ux * 14 * ss + uy * 6 * ss, hy - uy * 14 * ss - ux * 6 * ss)], fill=(255, 106, 26))
    return res.resize(size, Image.LANCZOS)


def _b64png(img, quality_colors=None) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=7)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ================================================================== facts about the robot
FQBN = {"esp32_devkit": ("esp32:esp32:esp32", "esp32:esp32"), "esp32_s3_devkit": ("esp32:esp32:esp32s3", "esp32:esp32"),
        "morph_controller_pcba": ("esp32:esp32:esp32s3", "esp32:esp32"), "arduino_nano": ("arduino:avr:nano", "arduino:avr"),
        "rp2040_pico": ("rp2040:rp2040:rpipico", "rp2040:rp2040"),
        "openrb_150": ("OpenRB-150:samd:OpenRB-150", "OpenRB-150:samd")}


def _short(name: str) -> str:
    return re.split(r" \(|, ", str(name))[0]


def _joint_label(jn: str) -> str:
    m = re.match(r"j(\d)_(.*)", jn)
    if m:
        return f"J{m.group(1)} {m.group(2).replace('_', ' ')}"
    s = jn.replace("_", " ")
    return s[:1].upper() + s[1:]


class _Facts:
    def __init__(self, design, asm, mfg, efinal):
        self.design, self.asm, self.mfg, self.efinal = design, asm, mfg or {}, efinal or {}
        self.inst = mf._instances(asm)
        self.fast = mf.step_fasteners(asm)
        self.arm_joints = list(asm.meta.get("arm_joints") or [j.name for j in asm.joints if j.actuator and not j.mimic])
        acts = [j for j in asm.joints if j.actuator and not j.mimic]
        self.act_joints = [j.name for j in acts]
        order = [j for j in self.arm_joints if j in self.act_joints] + [j for j in self.act_joints if j not in self.arm_joints]
        self.servo_id = {}
        for a in (self.efinal.get("actuators") or (self.efinal.get("plan") or {}).get("actuators") or []):
            if a.get("joint") and a.get("id") is not None:
                self.servo_id[a["joint"]] = a["id"]
        self.id_rows = []                       # [(joint, id)] one row per physical servo
        for e in asm.meta.get("servo_ids") or []:
            jn = e.get("joint")
            if jn not in self.act_joints:       # tolerate short names ("roll") from generators
                jn = next((j for j in self.act_joints if jn and (j.endswith(jn) or jn in j)), jn)
            if e.get("id") is not None:
                self.id_rows.append((jn, e["id"]))
                self.servo_id.setdefault(jn, e["id"])
        if not self.id_rows:
            nxt = 1
            for jn in order:
                j = next(x for x in acts if x.name == jn)
                for _ in range(int((j.meta or {}).get("count", 1) or 1)):
                    self.id_rows.append((jn, self.servo_id.get(jn, nxt) if _ == 0 else nxt))
                    nxt = max(nxt, self.id_rows[-1][1]) + 1
                self.servo_id.setdefault(jn, self.id_rows[-1][1])
        self.act_count = mf.actuator_counts(asm)
        self.folded = {"wiring": [], "calibration": []}
        self.family = next((catalog.ACTUATORS[k].get("family") for k in self.act_count if k in catalog.ACTUATORS), "smart_bus")
        mods = [e["sku"] for e in self.inst.values() if e["sku"] in catalog.MODULES]
        ctrl = next((k for k in mods if catalog.MODULES[k].get("role") == "controller"), None)
        if not ctrl and self.efinal.get("config") == "custom_pcb":
            ctrl = "morph_controller_pcba"
        if not ctrl:
            idx = mf._catalog_name_index()
            for r in self.efinal.get("sourcing") or []:
                k = r.get("sku") or idx.get(mf._norm(r.get("item", "")))
                if k in catalog.MODULES and catalog.MODULES[k].get("role") == "controller":
                    ctrl = k
                    break
        self.controller = ctrl or "esp32_devkit"
        self.mfg_by_group = {e["group"]: e for e in self.mfg.get("parts", [])}
        self.part_group = {p.name: (p.group or p.name) for p in asm.parts}

    def joint_of_prefix(self, prefix: str):
        m = re.search(r"(?:^|_)(j\d)(?:_|$)", prefix)
        if m:
            for jn in self.act_joints:
                if jn.startswith(m.group(1) + "_"):
                    return jn
        if "grip" in prefix:
            return next((j for j in self.act_joints if j == "gripper"), None)
        return None

    @property
    def supply(self):
        for r in (self.efinal.get("power_budget") or {}).get("rails", []):
            src = str(r.get("source") or "")
            key = src.split(" ")[0]
            if key in catalog.MODULES:
                return _short(catalog.MODULES[key]["name"])
            if src:
                return f"{r.get('v', ''):g} V · {src}" if isinstance(r.get("v"), (int, float)) else src
        return "12 V 10 A desktop supply" if self.family != "hobby_pwm" else "6 V 10 A supply"


# ================================================================== steps
TIME = {"shcs": 0.45, "bhcs": 0.45, "fhcs": 0.5, "insert": 0.9, "nut": 0.35, "washer": 0.15, "standoff": 0.35,
        "setscrew": 0.4, "tie": 0.25, "other": 0.4}


def _tool_key(t: str) -> str:
    t = t.lower()
    for k in ("torque screwdriver", "soldering iron", "flush cutter", "multimeter", "cable tie", "crimp", "esd"):
        if k in t:
            return k
    return t


def _add_tools(tools, new):
    keys = {_tool_key(t) for t in tools}
    for t in new:
        if t and _tool_key(t) not in keys:
            tools.append(t)
            keys.add(_tool_key(t))
    return tools


def _tools_for(fasteners, extra=()):
    tools = []
    _add_tools(tools, list(extra))
    _add_tools(tools, [f.get("drive") for f in fasteners])
    if any(f.get("torque_nm") for f in fasteners):
        _add_tools(tools, ["torque screwdriver 0.2–2 N·m"])
    return tools


def _sentences(text):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", str(text).strip())
    return [p.strip() for p in parts if p.strip()]


def _qty_list(fl, kinds):
    sel = [f for f in fl if f["kind"] in kinds]
    return ", ".join(f"{f['qty']} × {f['spec']}" for f in sel)


def _mech_steps(F: _Facts):
    asm, design = F.asm, F.design
    steps = []
    by_step = OrderedDict()
    for s in sorted(asm.steps, key=lambda s: s.index):
        by_step[s.index] = s
    if not by_step:
        for idx in sorted({p.step for p in asm.parts}):
            by_step[idx] = type("S", (), {"index": idx, "title": f"Step {idx}", "parts": [], "fasteners": [], "tools": [],
                                          "instructions": "", "subassembly": "", "camera": {}})()
    for idx, s in by_step.items():
        names = list(s.parts or [p.name for p in asm.parts if p.step == idx])
        parts = [asm.part(n) for n in names if any(p.name == n for p in asm.parts)]
        fl = [dict(f) for f in F.fast.get(idx, [])]
        if not parts and not fl and not s.instructions:
            continue
        if not parts and not fl:                # generator's own wiring / check steps: fold into the bring-up steps
            low = s.title.lower()
            kind = "wiring" if any(k in low for k in ("wiring", "wire", "cable", "harness", "bus")) else \
                "calibration" if any(k in low for k in ("calibrat", "check", "zero", "power", "test")) else None
            if kind:
                F.folded[kind].append({"title": s.title, "sentences": _sentences(s.instructions), "tools": list(s.tools or [])})
                continue
        table, sent, warn = OrderedDict(), [], []
        inst = [(k, e) for k, e in F.inst.items() if any(n in names for n in e["parts"])]
        fab = [p for p in parts if p.process in mf.FAB_PROCESSES and p.kind not in ("fastener", "cable")]
        # parts table
        for k, e in inst:
            nm, src = _sku_name(e["sku"])
            key = ("buy", e["sku"])
            table.setdefault(key, {"item": nm, "qty": 0, "source": src, "sku": e["sku"], "group": e["sku"]})
            table[key]["qty"] += e.get("n", 1)
        for p in fab:
            g = p.group or p.name
            me = F.mfg_by_group.get(g, {})
            key = ("make", g)
            src = f"{mf.PROC_LABEL.get(me.get('process', p.process), p.process)} · {me.get('material', p.material)}"
            table.setdefault(key, {"item": me.get("name") or mf.pretty(g), "qty": 0, "source": src, "group": g})
            table[key]["qty"] += 1
        # instructions
        if s.instructions:
            sent = _sentences(s.instructions)
        else:
            inserts = [f for f in fl if f["kind"] == "insert"]
            printed = [t["item"].lower() for (kind, g), t in table.items() if kind == "make"]
            if inserts and printed:
                sent.append(f"Press the {_qty_list(fl, ('insert',))} into the {', '.join(printed[:3])} with the soldering iron "
                            "at 245 °C; let each insert sink flush and square, then let the part cool for a minute.")
            for k, e in inst:
                if e["sku"] in catalog.ACTUATORS:
                    jn = F.joint_of_prefix(k)
                    nm = _short(catalog.ACTUATORS[e["sku"]]["name"])
                    if jn:
                        sid = F.servo_id.get(jn)
                        sent.append(f"Fit the {_joint_label(jn)} servo ({nm}{f', bus ID {sid}' if sid else ''}) into its cradle, "
                                    "cable exit towards the cable channel, and secure the case.")
                    else:
                        sent.append(f"Fit the {nm} into its cradle and secure the case.")
            mods = [_short(catalog.MODULES[e["sku"]]["name"]) for k, e in inst if e["sku"] in catalog.MODULES
                    and e["kind"] in ("electronics", "pcb")]
            if mods:
                sent.append(f"Mount the {', '.join(mods)} on their standoffs in the base bay, USB / power connectors facing the "
                            "rear cut-out.")
            brg = [_short(catalog.BEARINGS[e["sku"]]["name"]) for k, e in inst if e["sku"] in catalog.BEARINGS]
            if brg:
                sent.append(f"Seat the {', '.join(brg)} on its spigot; it must turn freely without rocking.")
            other = [t["item"] for (kind, g), t in table.items() if kind == "make"]
            if other:
                screws = _qty_list(fl, ("shcs", "bhcs", "fhcs", "nut", "setscrew"))
                sent.append(f"Fit the {', '.join(x.lower() for x in other[:4])}" + (f" and fasten with {screws}" if screws else "")
                            + ", tightening in a cross pattern.")
            if any("pad" in t["item"].lower() for t in table.values()):
                sent.append("Slide each sensor pad onto its finger's dovetail with the FSR tail through the slot; the pad must "
                            "sit flush and not preload the sensor.")
            if any(e["sku"] in catalog.ACTUATORS for k, e in inst):
                sent.append("Turn the new joint by hand through its full range: it must not bind or pinch a cable.")
            if not sent:
                sent.append(f"Install the {', '.join(t['item'] for t in table.values()) or 'parts'} as shown.")
        if any(f.get("torque_nm") for f in fl):
            tq = ", ".join(f"{f['spec']} {f['torque_nm']:g} N·m" for f in fl if f.get("torque_nm"))
            sent.append(f"Torque: {tq}. Into printed plastic, stop at the value; do not 'feel' it tighter.")
        # warnings
        if any(e["sku"] in catalog.ACTUATORS for k, e in inst):
            warn.append("Do not back-drive a servo quickly by hand: it generates voltage into the bus and wears the gear train.")
        if any(e["kind"] in ("electronics", "pcb") for k, e in inst):
            warn.append("ESD: touch a grounded surface before handling the boards; hold them by the edges.")
        if any(f["kind"] == "insert" for f in fl):
            warn.append("The insert tip is at 245 °C. Park the iron in its stand between inserts.")
        if any("pad" in t["item"].lower() for t in table.values()) and design.finger.sensor != "none":
            warn.append("Never crease the FSR within 5 mm of its active area; a kinked sensor reads high forever.")
        tools = _tools_for(fl, list(s.tools or []) + (["soldering iron + M3 insert tip"] if any(f["kind"] == "insert" for f in fl) else [])
                           + (["ESD wrist strap"] if any(e["kind"] in ("electronics", "pcb") for k, e in inst) else []))
        t_min = 3.0 + sum(TIME.get(f["kind"], 0.4) * f["qty"] for f in fl)
        t_min += sum(1.2 * t["qty"] if t["source"] != "purchased" else 2.0 * t["qty"] for t in table.values())
        steps.append({"index": int(idx), "title": s.title, "kind": "mechanical", "parts": names,
                      "parts_table": list(table.values()),
                      "fasteners": [{"spec": f["spec"], "qty": int(f["qty"]), "torque_nm": f.get("torque_nm"),
                                     "drive": f.get("drive"), "label": f.get("label"), "kind": f["kind"],
                                     "source": f.get("source", "")} for f in fl],
                      "tools": tools, "instructions": " ".join(sent), "substeps": sent, "warnings": warn,
                      "time_min": int(math.ceil(t_min)), "subassembly": getattr(s, "subassembly", "")})
    return steps


def _harness_rows(F: _Facts):
    h = F.efinal.get("harness") or []
    if h:
        return [{"name": w.get("name", f"W{i + 1}"), "from": w.get("from", ""), "to": w.get("to", ""),
                 "length_mm": int(round(float(w.get("length_mm", 0) or 0))), "conductors": w.get("conductors", ""),
                 "wire": w.get("wire", ""), "connector_a": w.get("connector_a", ""), "connector_b": w.get("connector_b", "")}
                for i, w in enumerate(h)]
    return [{"name": f"W{i + 1} {c.name}", "from": c.src, "to": c.dst, "length_mm": int(round(F.asm.cable_length(c))),
             "conductors": c.conductors, "wire": "", "connector_a": "", "connector_b": ""} for i, c in enumerate(F.asm.cables)]


def _elec_steps(F: _Facts, start: int):
    d = F.design
    rows = _harness_rows(F)
    bus = F.efinal.get("bus") or ("pwm" if F.family == "hobby_pwm" else "ttl_half_duplex")
    steps = []
    idx = start
    # ---------------------------------------------------------------- wiring
    order = [w["name"] for w in rows]
    chain = " → ".join(_joint_label(j) for j in F.act_joints) if bus != "pwm" else ""
    sent = ["Lay the harness out on the bench and check every wire against the table below before routing it."]
    for fo in F.folded["wiring"]:
        sent += fo["sentences"]
    if F.mfg_by_group.get("harness"):
        sent.append("Crimp any wires you make yourself from harness_cutlist.csv: strip 2.5 mm, crimp, tug-test each pin.")
    if bus == "pwm":
        sent.append("Plug each servo into its PWM channel on the driver board (brown/black = GND to the outside edge).")
    elif not F.folded["wiring"]:
        sent.append(f"Daisy-chain the servo bus from the controller through every joint ({chain}); each servo has two "
                    "identical bus sockets, so either one can be the input.")
    if not F.folded["wiring"]:
        sent += ["Route each run along the link's cable channel and leave a 30 mm service loop at every joint so the cable "
                 "never pulls tight at the end of travel.",
                 "Connect the force-sensor leads (if fitted) to their inputs, then fix the harness with cable ties every 60–80 mm."]
    sent.append("Plug the power lead into the inlet last; keep the supply unplugged.")
    n_ties = max(6, 2 * len(rows))
    steps.append({"index": idx, "title": "Wiring and cable routing", "kind": "wiring",
                  "parts": [c.name for c in F.asm.cables],
                  "parts_table": [{"item": w["name"], "qty": 1, "source": f"{w['length_mm']} mm · {w['conductors']}-wire",
                                   "group": "harness"} for w in rows],
                  "fasteners": [{"spec": "cable tie 100 mm", "qty": n_ties, "torque_nm": None, "drive": "flush cutters",
                                 "label": "cable tie 100 mm", "kind": "tie", "source": "inferred"}],
                  "tools": _add_tools(["wire stripper", "JST / Molex crimp tool", "flush cutters", "multimeter"],
                                      [t for fo in F.folded["wiring"] for t in fo["tools"]]),
                  "instructions": " ".join(sent), "substeps": sent,
                  "warnings": ["Check the power lead polarity with the multimeter before it touches the controller: "
                               "reversed 12 V destroys the regulator and every servo on the bus.",
                               "Never route a cable across a joint axis without a service loop."],
                  "time_min": int(10 + 4 * len(rows)), "harness": rows, "order": order})
    idx += 1
    # ---------------------------------------------------------------- first power-on safety
    psu = F.supply
    peak = sum(r.get("peak_a", 0) for r in (F.efinal.get("power_budget") or {}).get("rails", [])[:1]) or None
    checks = [
        "Workspace clear: nothing within the arm's reach + 100 mm, the robot clamped or screwed to the bench.",
        "Supply OFF. Measure resistance between V+ and GND at the inlet: it must read > 1 kΩ (no short).",
        "Set a bench supply to 12 V with a 1.5 A current limit (or use the PSU through a fused lead) for the first power-up.",
        "Power on. The controller LED lights, no servo moves, the supply stays out of current limit, nothing gets warm in 30 s.",
        "Measure the logic rail at the controller (5 V ±5 %) and the servo bus voltage at the last servo.",
        "Keep a hand on the supply switch / e-stop for every first move from here on."]
    steps.append({"index": idx, "title": "First power-on safety checks", "kind": "safety", "parts": [], "parts_table": [],
                  "fasteners": [], "tools": ["multimeter", "bench power supply with current limit"],
                  "instructions": " ".join(checks), "substeps": checks, "checklist": True,
                  "warnings": ["If anything smells hot or the supply hits its current limit: switch off at once and re-check the "
                               "wiring table." + (f" Expected peak bus current is {peak:.1f} A." if peak else ""),
                               "First moves at 20 % speed and reduced torque limit only."],
                  "time_min": 12, "supply": psu})
    idx += 1
    # ---------------------------------------------------------------- firmware + IDs
    fqbn, core = FQBN.get(F.controller, ("esp32:esp32:esp32", "esp32:esp32"))
    fw = F.efinal.get("firmware") or "firmware/morph_arm/morph_arm.ino"
    sketch = os.path.dirname(fw) or fw
    port = "/dev/ttyUSB0" if "esp32" in fqbn and "s3" not in fqbn else "/dev/ttyACM0"
    cmds = [f"arduino-cli core install {core}", f"arduino-cli compile --fqbn {fqbn} {sketch}",
            f"arduino-cli upload -p {port} --fqbn {fqbn} {sketch}", f"arduino-cli monitor -p {port} -c baudrate=115200"]
    ctrl_name = _short(catalog.MODULES.get(F.controller, {}).get("name", F.controller))
    if F.family == "dynamixel":
        id_tool = "DYNAMIXEL Wizard 2.0 (ROBOTIS)"
    elif F.family in ("smart_bus", "smart_bus_7v4"):
        id_tool = "Feetech FD software (or the firmware's ID-setup mode)"
    else:
        id_tool = None
    sent = [f"Connect the {ctrl_name} to the computer with a USB-C data cable (servo supply still off).",
            f"Install the board core, then compile and upload {fw} with the commands below."]
    if id_tool:
        sent.append(f"Set servo bus IDs one servo at a time with {id_tool}: connect only that servo, assign its ID from the "
                    "table, power-cycle, repeat. Two servos with the same ID corrupt the bus.")
    else:
        sent.append("Check each servo is on the PWM channel listed in the table.")
    sent.append("Open the serial monitor at 115200 baud and power the servo supply: the boot log must list every servo "
                "and both force-sensor channels.")
    ids = [{"joint": jn, "label": _joint_label(jn) + (f" ({k + 1})" if sum(1 for a, _ in F.id_rows if a == jn) > 1 else ""),
            "id": sid, "actuator": next((j.actuator for j in F.asm.joints if j.name == jn), "")}
           for jn, sid, k in ((jn, sid, [x for x, _ in F.id_rows[:i]].count(jn)) for i, (jn, sid) in enumerate(F.id_rows))]
    steps.append({"index": idx, "title": "Flash the firmware and set servo IDs", "kind": "firmware", "parts": [],
                  "parts_table": [], "fasteners": [], "tools": ["USB-C data cable", "computer with arduino-cli / Arduino IDE 2"],
                  "instructions": " ".join(sent), "substeps": sent, "commands": cmds, "servo_ids": ids,
                  "warnings": ["Upload with the servo supply OFF; some USB ports cannot source the inrush of the bus."],
                  "time_min": 20 + 2 * len(ids)})
    idx += 1
    # ---------------------------------------------------------------- calibration
    obj = d.object
    target = d.electronics.target_force_n or (obj.max_force_n / max(d.safety_factor, 1.0) if obj.max_force_n else 0.0)
    sent = ["Joint zero: drive each joint to its alignment marks (arm straight up, wrist level), then store the encoder "
            "offsets as the zero pose.",
            f"Gripper zero: close the jaws on a {obj.diameter_mm:g} mm gauge (the {obj.name}'s diameter); store this as "
            "gripper = 0 mm (contact).",
            "Force sensors: rest 50 g, 100 g, 200 g and 500 g on each pad (pad facing up) and record the raw reading; the "
            "firmware fits the curve from the table.",
            (f"Set the grip force limit to {target:.1f} N per finger ({obj.name}: damage above {obj.max_force_n:g} N)."
             if target else "Set the grip force limit for the object.")]
    for fo in F.folded["calibration"]:
        if any("zero" in x.lower() for x in fo["sentences"]):
            sent = [x for x in sent if not x.startswith("Joint zero")]
        sent = fo["sentences"] + sent
    steps.append({"index": idx, "title": "Calibrate joints, gripper and force sensors", "kind": "calibration", "parts": [],
                  "parts_table": [], "fasteners": [],
                  "tools": _add_tools(["kitchen scale or calibration weights (50–500 g)", f"{obj.diameter_mm:g} mm gauge (printed or a dowel)"],
                                      [t for fo in F.folded["calibration"] for t in fo["tools"]]),
                  "instructions": " ".join(sent), "substeps": sent, "cal_weights_g": [50, 100, 200, 500],
                  "grip_target_n": round(target, 2),
                  "warnings": ["Re-run the force-sensor calibration after a 10 minute warm-up: FSR readings drift with temperature."],
                  "time_min": 25})
    idx += 1
    # ---------------------------------------------------------------- first pick
    t = d.task
    sent = [f"Place a hard-boiled egg (or a {obj.diameter_mm:g} mm ball) at the pick point x = {t.pick_x_mm:g} mm, "
            f"y = {t.pick_y_mm:g} mm from the base axis; mark the place point x = {t.place_x_mm:g}, y = {t.place_y_mm:g} mm.",
            "Run the pick-and-place demo at 25 % speed; watch the grip-force readout stay below the limit.",
            f"Raise the speed step by step to 100 % (target cycle {t.cycle_time_s:g} s), then repeat with a raw egg."]
    steps.append({"index": idx, "title": "First pick-and-place test", "kind": "test", "parts": [], "parts_table": [],
                  "fasteners": [], "tools": [], "instructions": " ".join(sent), "substeps": sent,
                  "warnings": ["Stand clear of the arm's reach while it runs; the e-stop / supply switch stays within reach."],
                  "time_min": 10})
    return steps


# ================================================================== illustrations
def _posed(asm, q):
    T = asm.world_transforms(q)
    return T


def _world_meshes(asm, q):
    T = asm.world_transforms(q)
    out = {}
    for p in asm.parts:
        m = p.mesh.copy()
        m.apply_transform(T[p.link])
        out[p.name] = m
    return out, T


def _image_jobs(F: _Facts, steps, size=(800, 505)):
    """Every illustration as a render job: {key: kwargs}. Step images ghost earlier steps and zoom onto small
    new parts; electrical steps show the finished robot; the cover uses the 'ready' pose."""
    asm = F.asm
    world, T = _world_meshes(asm, dict(asm.meta.get("home") or {}))
    cache = {}
    jobs = OrderedDict()
    for s in [s for s in steps if s["kind"] == "mechanical"]:
        cur = set(s["parts"])
        items, focus_pts, arrows = [], [], []
        for p in asm.parts:
            if p.step < s["index"]:
                items.append((world[p.name], p.color, "ghost"))
            elif p.name in cur:
                m = world[p.name]
                ex = np.asarray(p.explode, float)
                if np.linalg.norm(ex) > 1e-3:
                    off = T[p.link][:3, :3] @ ex
                    m2 = m.copy()
                    m2.apply_translation(off)
                    c = m.bounds.mean(0)
                    if p.kind not in ("fastener",) and len(arrows) < 6:
                        arrows.append((c + off * 0.85, c + off * 0.15))
                    m = m2
                items.append((m, p.color, "focus"))
                focus_pts.append(m.bounds)
        if not items:
            continue
        allb = np.vstack([m.bounds for m, _, _ in items])
        fb = np.vstack(focus_pts) if focus_pts else allb
        span_f = np.linalg.norm(fb.max(0) - fb.min(0))
        span_a = np.linalg.norm(allb.max(0) - allb.min(0))
        frame = None
        if focus_pts and span_f < 0.45 * span_a:
            c = (fb.max(0) + fb.min(0)) / 2
            h = max(span_f * 1.1, span_a * 0.28) / 2
            frame = np.array([[c[0] + sx * h, c[1] + sy * h, c[2] + sz * h]
                              for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
        jobs[("step", s["index"])] = dict(items=items, size=size, focus_frame=frame, arrows=arrows, shadow=frame is None,
                                          cache=cache, ss=SS_STEP)
    if asm.parts:
        # wiring: the finished robot ghosted, cable routes drawn over it
        routes = []
        palette = ["#ff6a1a", "#2b6cd4", "#1f8a4c", "#8e44ad", "#c0392b"]
        for i, c in enumerate(asm.cables):
            pts = [(T[l] @ np.r_[np.asarray(x, float), 1.0])[:3] for l, x in c.points if l in T]
            if len(pts) > 1:
                routes.append((pts, palette[i % len(palette)], 3.0))
        jobs[("step", "wiring")] = dict(items=[(world[p.name], p.color, "ghost" if p.kind not in ("electronics", "pcb") else "normal")
                                               for p in asm.parts], size=size, cache=cache, lines=routes, ss=SS_STEP)
        d = F.design.task
        z0 = min(float(m.bounds[0][2]) for m in world.values())
        jobs[("step", "test")] = dict(items=[(world[p.name], p.color, "normal") for p in asm.parts], size=size, cache=cache, ss=SS_STEP,
                                      dots=[((d.pick_x_mm, d.pick_y_mm, z0), "#1f8a4c", 16, "pick"),
                                            ((d.place_x_mm, d.place_y_mm, z0), "#2b6cd4", 16, "place")],
                                      focus_frame=np.vstack([np.vstack([m.bounds for m in world.values()]),
                                                             [[d.pick_x_mm, d.pick_y_mm, z0], [d.place_x_mm, d.place_y_mm, z0]]]))
        jobs[("cover",)] = dict(items=[(m, p.color, "normal") for p, m in zip(asm.parts, _cover_pose(F))],
                                view=(32.0, 18.0), size=(1000, 700), bg=(246, 247, 249), pad=0.04)
    return jobs


def _cover_pose(F):
    """The 'ready' pose when it shows the arm articulated, else 'home' (a straight-up arm reads as a stick)."""
    asm = F.asm
    ready = dict(asm.meta.get("ready") or {})
    arm = [j for j in F.arm_joints if not j.startswith("j1")]
    if not any(abs(ready.get(j, 0.0)) > 0.25 for j in arm):
        ready = {**ready, **dict(asm.meta.get("home") or {})}
    world, _ = _world_meshes(asm, ready)
    return [world[p.name] for p in asm.parts]


def _thumb(mesh, color, size=(180, 130)):
    m = mesh.copy()
    m.apply_translation(-m.bounds.mean(0))
    return render([(m, color, "normal")], view=(35.0, 28.0), size=size, ss=2, pad=0.1, shadow=False, bg=(255, 255, 255))


# ================================================================== HTML
CSS = r"""
:root{--ink:#18202a;--ink2:#3b4654;--muted:#6b7684;--line:#e2e6eb;--line2:#cfd5dc;--bg:#e9ecf0;--paper:#fff;
--acc:#ff6a1a;--acc2:#2b6cd4;--acc-bg:#fff2ea;--warn:#8a5a00;--warn-bg:#fff8e6;--warn-line:#f2c94c;--ok:#1f8a4c;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:"Inter","Segoe UI",system-ui,-apple-system,Roboto,"Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:10.5pt;line-height:1.45;
-webkit-font-smoothing:antialiased;font-feature-settings:"tnum" 1,"cv11" 1}
.sheet{width:210mm;min-height:297mm;margin:22px auto;background:var(--paper);padding:15mm 15mm 16mm;position:relative;
box-shadow:0 1px 2px rgba(20,30,45,.08),0 8px 28px rgba(20,30,45,.10);border-radius:2px}
.runhead{position:absolute;top:7mm;left:15mm;right:15mm;display:flex;justify-content:space-between;font-size:7.5pt;
color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.runfoot{position:absolute;bottom:7mm;left:15mm;right:15mm;display:flex;justify-content:space-between;font-size:7.5pt;color:var(--muted)}
h1,h2,h3{margin:0;font-weight:700;letter-spacing:-.01em}
h2{font-size:17pt;margin:4mm 0 3mm}
h3{font-size:10.5pt;margin:4.2mm 0 1.6mm}
.kicker{font-size:8pt;font-weight:700;color:var(--acc);letter-spacing:.14em;text-transform:uppercase}
.muted{color:var(--muted)} .mono{font-family:var(--mono);font-size:.92em}
table{border-collapse:collapse;width:100%}
th{font-size:7.6pt;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600;text-align:left;
padding:5px 6px;border-bottom:1.5px solid var(--ink)}
td{padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top;font-size:9.4pt}
td.n,th.n{text-align:right;white-space:nowrap}
/* cover */
.cover{display:flex;flex-direction:column;padding:0;overflow:hidden}
.cover .band{background:#f6f7f9;padding:14mm 15mm 0;position:relative}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.24em;font-size:10pt}
.brand i{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--acc),#ff9a4a);display:inline-block;
box-shadow:inset 0 0 0 2px rgba(255,255,255,.35)}
.cover h1{font-size:34pt;line-height:1.05;margin-top:3mm;letter-spacing:-.025em}
.cover .sub{font-size:13pt;color:var(--ink2);margin-top:2mm}
.cover .hero{display:block;width:100%;max-height:118mm;object-fit:contain;margin-top:1mm}
.cover .meta{padding:7mm 15mm 0;display:grid;grid-template-columns:1.45fr 1fr;gap:9mm}
.spec td{padding:3.5px 0;border-bottom:1px solid var(--line);font-size:9pt}
.spec td:first-child{color:var(--muted);width:38%}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-content:start}
.tile{border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.tile .v{font-size:15pt;font-weight:700;letter-spacing:-.02em;white-space:nowrap}
.tile .l{font-size:7.5pt;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.docid{padding:6mm 15mm 9mm;margin-top:auto;display:flex;justify-content:space-between;gap:10mm;font-size:7.6pt;color:var(--muted)}
/* contents */
.toc td{padding:3px 6px;font-size:9.1pt}
.toc .no{width:9mm;font-weight:700;color:var(--acc)}
.toc .t{font-weight:600}
.pill{display:inline-block;font-size:7.5pt;padding:1px 7px;border-radius:999px;background:#eef1f5;color:var(--ink2);
text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.pill.mech{background:#eef3fb;color:#2856a3}.pill.wiring{background:#fff1e8;color:#b5470c}.pill.safety{background:#fdecec;color:#a42121}
.pill.firmware{background:#eef8f1;color:#1b7a43}.pill.calibration{background:#f4effc;color:#6237a8}.pill.test{background:#eef1f5;color:#3b4654}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8mm}
.note{border-left:3px solid var(--acc2);background:#f3f7fd;padding:8px 11px;border-radius:0 6px 6px 0;font-size:9.3pt;margin:3mm 0}
.warn{border-left:3px solid var(--warn-line);background:var(--warn-bg);color:#5b3d00;padding:6px 10px 6px 34px;
border-radius:0 6px 6px 0;font-size:9.2pt;margin:2.2mm 0;position:relative}
.warn:before{content:"!";position:absolute;left:10px;top:7px;width:16px;height:16px;border-radius:4px;background:#f2a516;color:#fff;
font-weight:800;font-size:10px;display:flex;align-items:center;justify-content:center;transform:rotate(0)}
/* kit */
.kit{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
.kit.f{grid-template-columns:repeat(8,1fr)}
.kit.f .k img{padding:0 6px}
.kit .k{border:1px solid var(--line);border-radius:8px;padding:6px 7px 7px;position:relative;break-inside:avoid}
.kit .k img{width:100%;height:auto;display:block}
.kit .k .ph{aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;color:#9aa3ad}
.kit .k .ph svg{width:44%;height:auto}
.kit .k .q{position:absolute;top:5px;right:7px;font-weight:800;font-size:10pt}
.kit .k .nm{font-size:7.8pt;font-weight:600;line-height:1.22;margin-top:2px}
.kit .k .s{font-size:7.4pt;color:var(--muted)}
.tools{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.tool{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:8px;padding:5px 7px;font-size:8.2pt;line-height:1.25}
.tool svg{flex:0 0 18px;height:18px;color:var(--ink2)}
/* steps */
.stephead{display:flex;align-items:flex-end;gap:12px;border-bottom:2px solid var(--ink);padding-bottom:3mm;margin-top:2mm}
.stepno{font-size:42pt;font-weight:800;line-height:.85;letter-spacing:-.04em;color:var(--ink);min-width:15mm;padding-right:2mm}
.stephead .t{flex:1}
.stephead h2{margin:1mm 0 0;font-size:16pt}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:1.5mm}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:8pt;color:var(--ink2);background:#f1f3f6;border-radius:999px;padding:2px 9px}
.chip b{color:var(--ink)}
.fig{position:relative;margin:4mm 0 2mm;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
.fig img{display:block;width:100%;height:auto}
.callout{position:absolute;top:10px;right:10px;background:rgba(255,255,255,.94);border:1.5px solid var(--ink);border-radius:8px;
padding:6px 8px;display:flex;gap:10px;align-items:flex-end;z-index:2;max-width:70%;flex-wrap:wrap;justify-content:flex-end}
.callout .f{text-align:center;font-size:7.5pt;line-height:1.15;flex:0 0 auto;white-space:nowrap}
.callout .f img{height:38px;width:auto;display:block;margin:0 auto 2px}
.callout .f b{font-size:10pt}
.cols{display:grid;grid-template-columns:0.9fr 1.1fr;gap:7mm;margin-top:3mm}
.cols.top{grid-template-columns:1.05fr 0.95fr;margin-top:4mm}
.cols.top .fig{margin-top:0}
table.harn td{font-size:8.3pt;padding:4px 5px}
table.harn td.mono{font-size:7.6pt}
ol.ins{margin:0;padding:0;list-style:none;counter-reset:i}
ol.ins li{counter-increment:i;position:relative;padding:0 0 2.6mm 9mm;font-size:9.8pt}
.cols.top ol.ins li{font-size:9.2pt;padding-bottom:2mm}
ol.ins li:before{content:counter(i);position:absolute;left:0;top:0;width:6mm;height:6mm;border-radius:50%;background:var(--ink);
color:#fff;font-size:8pt;font-weight:700;display:flex;align-items:center;justify-content:center}
.check li:before{content:"";background:#fff;border:1.6px solid var(--ink);border-radius:3px;width:4.2mm;height:4.2mm;top:.4mm}
.fast td{font-size:8.8pt;padding:3.4px 6px}
.fast .tq{font-weight:700;color:var(--acc)}
pre.cmd{background:#0f1720;color:#e6edf3;font-family:var(--mono);font-size:8.6pt;padding:9px 11px;border-radius:8px;margin:2mm 0;
white-space:pre-wrap;line-height:1.55}
pre.cmd .p{color:#7ee2a8}
.cal td{height:8mm}
.sign{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8mm;margin-top:8mm}
.sign div{border-top:1px solid var(--ink);padding-top:2px;font-size:8pt;color:var(--muted)}
.chain{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:2mm 0 1mm}
.chain span{background:#fff;border:1.5px solid var(--ink);border-radius:6px;padding:2px 7px;font-size:8.2pt;font-weight:600}
.chain i{color:var(--acc);font-style:normal;font-weight:800}
.sheet.step{height:297mm;display:flex;flex-direction:column}
.sheet.step .fig.flex{flex:1 1 auto;min-height:62mm;max-height:122mm;margin:4mm 0 1mm}
.sheet.step .fig.flex > img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.sheet.step .cols{flex:0 0 auto}
.fig.small > img{max-height:62mm;object-fit:contain}
.grid2.tight{gap:0 4mm}.grid2.tight .warn{margin:1.4mm 0;font-size:8.6pt}
@page{size:A4;margin:0}
@media print{
 html,body{background:#fff}
 .sheet{margin:0;box-shadow:none;border-radius:0;break-after:page;page-break-after:always;width:210mm;min-height:297mm}
 .fig,.k,.tile,.warn,table tr{break-inside:avoid}
 a{color:inherit;text-decoration:none}
}
@media screen and (max-width:820px){.sheet{width:auto;min-height:0;margin:10px;padding:10mm 6mm}
 .sheet.step{height:auto}.sheet.step .fig.flex{height:62vw}
 .cover .meta,.grid2,.cols{grid-template-columns:1fr}.kit{grid-template-columns:repeat(2,1fr)}.tools{grid-template-columns:1fr 1fr}}
"""

ICONS = {
    "hex": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4l3 3-1 1 9 9 2-2-9-9 1-1-3-3z"/><path d="M15 15l4 4"/></svg>',
    "torque": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="8" cy="8" r="4"/><path d="M11 11l9 9"/><path d="M8 4v2M12 8h-2"/></svg>',
    "iron": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 21l6-6"/><rect x="9" y="6" width="5" height="11" rx="1.5" transform="rotate(45 11.5 11.5)"/><path d="M15 9l5-5"/></svg>',
    "meter": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="5" y="2.5" width="14" height="19" rx="2"/><rect x="8" y="5.5" width="8" height="5" rx="1"/><circle cx="12" cy="15.5" r="2.5"/></svg>',
    "usb": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="5" width="18" height="11" rx="1.5"/><path d="M8 20h8M12 16v4"/></svg>',
    "crimp": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 20l7-7M20 20l-7-7"/><path d="M9 11l3-7 3 7"/></svg>',
    "cut": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="6" cy="18" r="3"/><circle cx="18" cy="18" r="3"/><path d="M8 16L18 4M16 16L6 4"/></svg>',
    "scale": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20h16l-2-11H6z"/><circle cx="12" cy="5" r="2"/></svg>',
    "strap": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="8" cy="12" r="4"/><path d="M12 12h8M17 9v6"/></svg>',
    "power": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M13 2L5 14h6l-1 8 8-12h-6z"/></svg>',
    "gauge": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/></svg>',
    "nut": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><circle cx="12" cy="12" r="3"/></svg>',
    "tie": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 18c3-9 9-14 16-14"/><path d="M6.5 13.5l2 1M8.5 10.5l2 1M11 8l2 1M14 6l1.8 1.2"/><rect x="2.5" y="17" width="4" height="4" rx="1"/></svg>',
}


def _icon_for(tool: str) -> str:
    t = tool.lower()
    for k, key in (("torque", "torque"), ("hex", "hex"), ("solder", "iron"), ("multimeter", "meter"), ("usb", "usb"),
                   ("computer", "usb"), ("crimp", "crimp"), ("strip", "crimp"), ("cutter", "cut"), ("scale", "scale"),
                   ("weights", "scale"), ("esd", "strap"), ("supply", "power"), ("gauge", "gauge"), ("nut driver", "nut")):
        if k in t:
            return ICONS[key]
    return ICONS["hex"]


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_min(m) -> str:
    m = int(round(m))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _fastener_mesh(f):
    from . import components
    kind = f.get("kind")
    spec = f"{f.get('size') or 'M3'}x{(f.get('length') or 8):g}"
    try:
        if kind in ("shcs", "bhcs", "fhcs", "nut", "washer", "insert"):
            m = components.fastener(spec, kind)
            return m, components.FASTENER_LOOK.get(kind, ("#2b2c30", "Steel"))[0]
        if kind == "standoff":
            return components.hex_prism(5.0, f.get("length") or 6.0, 0.0, hole=2.5), "#c9ccd1"
    except Exception:
        return None, None
    return None, None


def _sku_name(sku: str):
    """Display name + source line for a purchased sku (catalog entry, servo horn, hardware, or anything else)."""
    try:
        return _short(catalog.get(sku)["name"]), "purchased"
    except KeyError:
        pass
    base = next((k for k in catalog.ACTUATORS if sku.startswith(k + "_") and "horn" in sku), None)
    if base:
        return f"{_short(catalog.ACTUATORS[base]['name'])} horn", "supplied with the servo"
    f = mf.parse_fastener(sku)
    if f["size"] and f["kind"] in ("standoff", "insert", "nut", "washer", "setscrew", "shcs", "bhcs", "fhcs"):
        return f["label"], "hardware"
    g = mf._GENERIC.get(next((k for k in mf._GENERIC if k in sku.lower()), ""), None)
    return (g["name"] if g else mf.pretty(sku)), "purchased"


def _drive_short(d: str) -> str:
    return str(d).replace(" hex key", " hex").replace("soldering iron + ", "iron + ").replace(" nut driver", " nut drv")


def _fig_style(s) -> str:
    """Shrink a mechanical step's illustration when the text below it would push the page past A4."""
    if s["kind"] != "mechanical":
        return ""
    left = 0
    if s.get("parts_table"):
        left += 52 + sum(26 if len(t["item"]) + len(t["source"]) < 44 else 42 for t in s["parts_table"])
    if s["fasteners"]:
        left += 52 + 26 * len(s["fasteners"])
    if s["tools"]:
        left += 40 + 24 * math.ceil(len(s["tools"]) / 2)
    right = 34 + sum(20 * math.ceil(len(x) / 52) + 9 for x in s["substeps"]) + sum(14 + 18 * math.ceil(len(w) / 55)
                                                                                    for w in s["warnings"])
    room = 900 - max(left, right)
    h = int(max(250, min(430, room)))
    return f' style="max-height:{h}px;width:auto;max-width:100%;margin:0 auto"' if h < 430 else ""


def _wire_short(name: str) -> str:
    out = []
    for tok in str(name).replace("→", "->").split():
        if "->" in tok or "." in tok:
            break
        out.append(tok)
    return " ".join(out) or str(name)


def _ph(kind="") -> str:
    icon = ICONS["tie"] if kind == "tie" else ICONS["nut"]
    return f'<div class="ph">{icon}</div>'


def _img(src, alt="") -> str:
    return f'<img src="{src}" alt="{_e(alt)}">' if src else ""


def _robot_title(design) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_\s]+", design.name) if w) or "Robot"


def _sheet(inner, head_l, head_r, foot_l, page, cls=""):
    return (f'<section class="sheet {cls}"><div class="runhead"><span>{_e(head_l)}</span><span>{_e(head_r)}</span></div>{inner}'
            f'<div class="runfoot"><span>{_e(foot_l)}</span><span>{page}</span></div></section>')


def _html(F: _Facts, steps, tools, total_min, imgs, cover_png, thumbs, fthumbs, rev):
    d, asm, mfg = F.design, F.asm, F.mfg
    title = _robot_title(d)
    today = datetime.date.today().isoformat()
    head = f"{title} · assembly manual"
    pages = []
    pg = [0]

    def sheet(inner, cls=""):
        pg[0] += 1
        return _sheet(inner, head, f"Morph · rev {rev}", f"{title} — generated by Morph on {today}", pg[0], cls)

    # ------------------------------------------------------------------ cover
    acts = ", ".join(f"{n} × {_short(catalog.ACTUATORS.get(k, {}).get('name', k))}" for k, n in F.act_count.items())
    sz = asm.meta.get("sizing") or {}
    reach = sz.get("reach_mm") or d.arm.reach_mm
    mass = sum(asm.link_mass(l)[0] for l in asm.links)
    obj = d.object
    grip = "Rack-and-pinion parallel jaws" + (", FSR 402 force sensing" if d.finger.sensor == "fsr402" else "")
    cfg = mfg.get("config") or d.build.config
    cfgd = (mfg.get("configs") or {}).get(cfg, {})
    unit = (mfg.get("totals") or {}).get("unit_cost") or cfgd.get("unit_cost")
    spec_rows = [("Task", f"Pick and place a {obj.name} ({obj.diameter_mm:g} mm, {obj.mass_g:g} g)"
                  + (f" below {obj.max_force_n:g} N" if obj.max_force_n else "")),
                 ("Arm", f"{len(F.arm_joints) or d.arm.dof}-DOF, {float(reach):.0f} mm reach"),
                 ("Actuators", acts or "—"), ("Gripper", grip),
                 ("Controller", _short(catalog.MODULES.get(F.controller, {}).get("name", F.controller))),
                 ("Bus · supply", f"{'PWM' if F.family == 'hobby_pwm' else 'half-duplex TTL servo bus'} · {F.supply}"),
                 ("Build config", f"{cfg.capitalize()} × {d.build.quantity}" + (f" — {cfgd.get('summary')}" if cfgd.get("summary") else "")),
                 ("Robot mass", f"{mass:.2f} kg (estimated)")]
    n_fab = sum(e.get("qty", 0) for e in mfg.get("parts", []) if e.get("process") in mf.FAB_PROCESSES)
    n_fast = sum(f["qty"] for s in steps for f in s["fasteners"])
    tiles = [(len(steps), "steps"), (_fmt_min(total_min), "build time"),
             (n_fab or len({p.group or p.name for p in asm.parts if p.process in mf.FAB_PROCESSES}), "made parts"),
             (n_fast, "fasteners")]
    if unit:
        tiles[3] = (f"${unit:,.0f}", "unit cost (est.)")
    cover = (f'<section class="sheet cover"><div class="band"><div class="brand"><i></i>MORPH</div>'
             f'<div class="kicker" style="margin-top:9mm">Assembly · wiring · bring-up</div><h1>{_e(title)}</h1>'
             f'<div class="sub">{len(F.arm_joints) or d.arm.dof}-DOF arm with a force-sensing gripper</div>'
             f'<img class="hero" src="{cover_png}" alt="{_e(title)}"></div>'
             f'<div class="meta"><table class="spec">{"".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in spec_rows)}</table>'
             f'<div class="tiles">{"".join(f"<div class=tile><div class=v>{_e(v)}</div><div class=l>{_e(l)}</div></div>" for v, l in tiles)}</div></div>'
             f'<div class="docid"><span>Document MORPH-{_e(d.name.upper())}-ASM · rev {_e(rev)} · {today}</span>'
             f'<span>Every number in this manual is generated from the design; costs are estimates.</span></div></section>')
    pg[0] += 1
    pages.append(cover)
    # ------------------------------------------------------------------ kit (built first: its page count feeds the contents)
    fab_cards, buy_cards = [], []
    for e in mfg.get("parts", []):
        if e.get("process") not in mf.FAB_PROCESSES:
            continue
        im = thumbs.get(e["group"])
        fab_cards.append(f'<div class="k"><span class="q">×{e["qty"]}</span>{_img(im)}'
                         f'<div class="nm">{_e(e["name"])}</div><div class="s">{_e(mf.PROC_LABEL.get(e["process"], e["process"]))} · '
                         f'{_e(e.get("material", ""))}</div></div>')
    if not fab_cards:
        for g, im in thumbs.items():
            if g.startswith("buy:"):
                continue
            n = sum(1 for p in asm.parts if (p.group or p.name) == g)
            fab_cards.append(f'<div class="k"><span class="q">×{n}</span>{_img(im)}<div class="nm">{_e(mf.pretty(g))}</div></div>')
    cnt = Counter()
    for e in F.inst.values():
        cnt[e["sku"]] += e.get("n", 1)
    for k, n in F.act_count.items():
        if k in catalog.ACTUATORS:
            cnt[k] = max(cnt.get(k, 0), n)
    hw = Counter()
    for sku, n in cnt.items():
        if any(sku.startswith(k + "_") and "horn" in sku for k in catalog.ACTUATORS):
            continue                                   # horns ship with their servo
        try:
            nm = _short(catalog.get(sku)["name"])
            sub = "purchased" + (" · horn included" if sku in catalog.ACTUATORS else "")
        except KeyError:
            f = mf.parse_fastener(sku)
            if f["size"] and f["kind"] in ("standoff", "insert", "nut", "washer", "setscrew", "shcs", "bhcs", "fhcs"):
                hw[f["spec"]] += n
                continue
            nm, sub = _sku_name(sku)
        im = thumbs.get("buy:" + sku)
        buy_cards.append(f'<div class="k"><span class="q">×{n}</span>{_img(im)}'
                         f'<div class="nm">{_e(nm)}</div><div class="s">{_e(sub)}</div></div>')
    fast_tot = OrderedDict()
    for s in steps:
        for f in s["fasteners"]:
            k = f["spec"]
            fast_tot.setdefault(k, {**f, "qty": 0})
            fast_tot[k]["qty"] += f["qty"]
    for spec, n in hw.items():
        f = mf.parse_fastener(spec)
        fast_tot.setdefault(f["spec"], {**f, "qty": 0})
        fast_tot[f["spec"]]["qty"] = max(fast_tot[f["spec"]]["qty"], n)
    fcards = []
    for k, f in fast_tot.items():
        im = fthumbs.get(k)
        fcards.append(f'<div class="k"><span class="q">×{f["qty"]}</span>{_img(im) or _ph(f["kind"])}'
                      f'<div class="nm">{_e(f["spec"])}</div><div class="s">{_e(_drive_short(f.get("drive") or ""))}</div></div>')
    intro = (f'<div class="kicker" style="margin-top:4mm">Kit</div><h2>What is in the box</h2>'
             f'<p class="muted" style="margin:0 0 3mm">Lay every part out and tick it off before step 1. Fabricated parts are made '
             f'per the manufacturing plan ({_e(cfg)}); purchased parts and fasteners come from the BOM.</p>')
    sec_made = f'<h3>Made parts</h3><div class="kit">{"".join(fab_cards)}</div>'
    sec_buy = f'<h3>Purchased parts</h3><div class="kit">{"".join(buy_cards)}</div>'
    sec_fast = (f'<h3>Fasteners <span class="muted" style="font-weight:500">(order +10 % spares)</span></h3>'
                f'<div class="kit f">{"".join(fcards)}</div>')
    rows = lambda n, c: -(-n // c)                       # noqa: E731  (ceil)
    est = 150 + 60 * 3 + 128 * (rows(len(fab_cards), 6) + rows(len(buy_cards), 6)) + 100 * rows(len(fcards), 8)
    kit_pages = [intro + sec_made + sec_buy + sec_fast] if est <= 900 else \
        [intro + sec_made, '<div class="kicker" style="margin-top:4mm">Kit, continued</div><h2>Purchased parts and fasteners</h2>'
         + sec_buy + sec_fast]
    # ------------------------------------------------------------------ contents + before you start
    toc = "".join(f'<tr><td class="no">{i + 1}</td><td><span class="t">{_e(s["title"])}</span></td>'
                  f'<td><span class="pill {"mech" if s["kind"] == "mechanical" else s["kind"]}">{_e(s["kind"])}</span></td>'
                  f'<td class="n">{s["time_min"]} min</td><td class="n muted">p. {i + 3 + len(kit_pages)}</td></tr>' for i, s in enumerate(steps))
    tool_html = "".join(f'<div class="tool">{_icon_for(t)}<span>{_e(t)}</span></div>' for t in tools)
    safety = ["Read each step fully before starting it; the illustration shows the parts added in that step in colour, "
              "earlier parts in grey.",
              "Work on an ESD-safe, well-lit bench. Keep the power supply unplugged until the safety checks.",
              "Torque values are for screws into heat-set inserts or metal; never exceed them in printed plastic.",
              "The finished arm can move fast: clamp the base and keep the workspace clear when it is powered."]
    inner = (f'<div class="kicker" style="margin-top:4mm">Contents</div><h2>{len(steps)} steps · {_fmt_min(total_min)}</h2>'
             f'<table class="toc"><thead><tr><th></th><th>Step</th><th>Type</th><th class="n">Time</th><th class="n">Page</th></tr></thead>'
             f'<tbody>{toc}</tbody></table>'
             f'<h3>Tools you need</h3><div class="tools">{tool_html}</div>'
             f'<h3>Before you start</h3><div class="grid2 tight">' + "".join(f'<div class="warn">{_e(x)}</div>' for x in safety) + "</div>")
    pages.append(sheet(inner))
    for kp in kit_pages:
        pages.append(sheet(kp))
    # ------------------------------------------------------------------ steps
    for i, s in enumerate(steps):
        chips = [f'<span class="chip">⏱ <b>{s["time_min"]} min</b></span>',
                 f'<span class="pill {"mech" if s["kind"] == "mechanical" else s["kind"]}">{_e(s["kind"])}</span>']
        if s["fasteners"]:
            chips.append(f'<span class="chip"><b>{sum(f["qty"] for f in s["fasteners"])}</b> fasteners</span>')
        if s.get("parts_table"):
            n_pt = sum(t["qty"] for t in s["parts_table"])
            chips.append(f'<span class="chip"><b>{n_pt}</b> {"wires" if s["kind"] == "wiring" else "parts"}</span>')
        headh = (f'<div class="stephead"><div class="stepno">{i + 1}</div><div class="t"><div class="kicker">Step {i + 1} of {len(steps)}</div>'
                 f'<h2>{_e(s["title"])}</h2><div class="chips">{"".join(chips)}</div></div></div>')
        fig = ""
        im = imgs.get(s["index"])
        if im:
            call = ""
            fsel = [f for f in s["fasteners"] if f["kind"] != "tie"][:4]
            if fsel:
                call = '<div class="callout">' + "".join(
                    f'<div class="f">{_img(fthumbs.get(f["spec"]))}<b>{f["qty"]}×</b><br>{_e(f["spec"])}</div>' for f in fsel) + "</div>"
            fig = f'<div class="fig{" flex" if s["kind"] == "mechanical" else " small"}"><img src="{im}" alt="step {i + 1}">{call}</div>'
        left = ""
        if s.get("parts_table") and s["kind"] == "mechanical":
            left += ('<h3>Parts</h3><table><thead><tr><th>Item</th><th class="n">Qty</th></tr></thead><tbody>'
                     + "".join(f'<tr><td>{_e(t["item"])} <span class="muted" style="font-size:7.8pt;white-space:nowrap">· {_e(t["source"])}</span></td>'
                               f'<td class="n"><b>{t["qty"]}</b></td></tr>' for t in s["parts_table"]) + "</tbody></table>")
        if s["fasteners"]:
            left += ('<h3>Fasteners</h3><table class="fast"><thead><tr><th>Fastener</th><th class="n">Qty</th><th class="n">Torque</th>'
                     '<th>Drive</th></tr></thead><tbody>'
                     + "".join(f'<tr><td class="mono">{_e(f["spec"])}</td><td class="n"><b>{f["qty"]}</b></td>'
                               f'<td class="n tq">{(str(f["torque_nm"]) + " N·m") if f.get("torque_nm") else "—"}</td>'
                               f'<td class="muted">{_e(_drive_short(f.get("drive") or ""))}</td></tr>' for f in s["fasteners"]) + "</tbody></table>")
        if s["tools"]:
            left += '<h3>Tools</h3><div class="chips">' + "".join(f'<span class="chip">{_e(t)}</span>' for t in s["tools"]) + "</div>"
        ol_cls = "ins check" if s.get("checklist") else "ins"
        right = f'<h3>{"Checklist" if s.get("checklist") else "Instructions"}</h3><ol class="{ol_cls}">' + \
                "".join(f"<li>{_e(x)}</li>" for x in s["substeps"]) + "</ol>"
        right += "".join(f'<div class="warn">{_e(w)}</div>' for w in s["warnings"])
        extra = ""
        if s["kind"] == "wiring":
            if F.family != "hobby_pwm" and F.act_joints:
                left += ('<h3>Bus order</h3><div class="chain"><span>controller</span>'
                         + "".join(f'<i>→</i><span>{_e(_joint_label(j))} · ID {sid}</span>' for j, sid in F.id_rows)
                         + "</div>")
            extra += ('<h3>Harness</h3><table class="harn"><thead><tr><th>Wire</th><th>From → to</th><th class="n">Length</th><th>Wire</th>'
                      '<th>Connectors</th></tr></thead><tbody>'
                      + "".join(f'<tr><td><b>{_e(_wire_short(w["name"]))}</b></td><td class="mono">{_e(w["from"])} → {_e(w["to"])}</td>'
                                f'<td class="n">{w["length_mm"]} mm</td><td>{_e(w["conductors"])}× {_e(w["wire"])}</td>'
                                f'<td class="muted">{_e(w["connector_a"])} / {_e(w["connector_b"])}</td></tr>' for w in s["harness"])
                      + "</tbody></table>")
        if s["kind"] == "firmware":
            extra += ('<h3>Commands</h3><pre class="cmd">' + "\n".join(f'<span class="p">$</span> {_e(c)}' for c in s["commands"]) + "</pre>")
            extra += ('<h3>Servo IDs</h3><table><thead><tr><th>Joint</th><th>Actuator</th><th class="n">Bus ID</th><th>Done</th></tr></thead><tbody>'
                      + "".join(f'<tr><td>{_e(x["label"])}</td><td>{_e(_short(catalog.ACTUATORS.get(x["actuator"], {}).get("name", x["actuator"])))}</td>'
                                f'<td class="n"><b>{_e(x["id"])}</b></td><td>☐</td></tr>' for x in s["servo_ids"]) + "</tbody></table>")
        if s["kind"] == "calibration":
            extra += ('<h3>Force-sensor log</h3><table class="cal"><thead><tr><th>Load</th><th>Left pad raw</th><th>Right pad raw</th>'
                      '<th>Note</th></tr></thead><tbody>'
                      + "".join(f"<tr><td><b>{g} g</b> ({g * 0.00981:.2f} N)</td><td></td><td></td><td></td></tr>" for g in s["cal_weights_g"])
                      + "</tbody></table>")
            extra += ('<h3>Joint zero offsets</h3><table class="cal"><thead><tr><th>Joint</th><th>Offset (ticks / deg)</th><th>Done</th>'
                      '</tr></thead><tbody>' + "".join(f"<tr><td>{_e(_joint_label(j))}</td><td></td><td>☐</td></tr>" for j in F.act_joints)
                      + "</tbody></table>")
        if s["kind"] == "mechanical" or not fig:
            body = headh + fig + f'<div class="cols"><div>{left}</div><div>{right}</div></div>' + extra
        else:
            body = headh + f'<div class="cols top"><div>{fig}{left}</div><div>{right}</div></div>' + extra
        pages.append(sheet(body, "step" if s["kind"] == "mechanical" and fig else ""))
    # ------------------------------------------------------------------ appendix
    tq_rows = "".join(f'<tr><td class="mono">{_e(size)}</td><td class="n tq"><b>{t:g} N·m</b></td>'
                      f'<td class="n">{catalog.FASTENER_SIZES.get(size, (0, 0, 0))[2]:g} mm</td></tr>'
                      for size, t in catalog.FASTENER_TORQUE_NM.items())
    ftot = "".join(f'<tr><td class="mono">{_e(f["spec"])}</td><td class="muted">{_e(_drive_short(f.get("drive") or ""))}</td>'
                   f'<td class="n"><b>{f["qty"]}</b></td><td class="n">{math.ceil(f["qty"] * 1.1)}</td></tr>' for f in fast_tot.values())
    inner = (f'<div class="kicker" style="margin-top:4mm">Appendix</div><h2>Reference</h2><div class="grid2"><div>'
             f'<h3>Torque table</h3><table class="fast"><thead><tr><th>Size</th><th class="n">Max torque</th><th class="n">Hex key</th></tr>'
             f'</thead><tbody>{tq_rows}</tbody></table><div class="note">Values are for screws into brass heat-set inserts or '
             f'metal. Into bare printed plastic use half.</div></div><div><h3>Fastener totals</h3><table class="fast"><thead><tr>'
             f'<th>Spec</th><th>Drive</th><th class="n">Need</th><th class="n">Order</th></tr></thead><tbody>{ftot}</tbody></table></div></div>'
             f'<h3>Build record</h3><table class="cal"><tbody><tr><td style="width:40%">Serial number</td><td></td></tr>'
             f'<tr><td>Build config / quantity</td><td>{_e(cfg)} × {d.build.quantity}</td></tr><tr><td>Firmware version</td><td></td></tr>'
             f'<tr><td>Assembly time (actual)</td><td></td></tr><tr><td>First successful pick</td><td></td></tr></tbody></table>'
             f'<div class="sign"><div>Assembled by</div><div>Checked by</div><div>Date</div></div>')
    pages.append(sheet(inner))
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)} — Assembly manual</title><style>{CSS}</style></head><body>" + "".join(pages) + "</body></html>")


# ================================================================== build
def build(design, asm, mfg, efinal, outdir, images: bool = True) -> dict:
    os.makedirs(outdir, exist_ok=True)
    F = _Facts(design, asm, mfg, efinal)
    steps = _mech_steps(F)
    start = max([s["index"] for s in steps] + [s.index for s in asm.steps] + [0]) + 1
    steps += _elec_steps(F, start)
    tools = []
    for s in steps:
        for t in s["tools"]:
            if t not in tools:
                tools.append(t)
    total = int(sum(s["time_min"] for s in steps))
    rev = os.path.basename(os.path.dirname(os.path.abspath(outdir)))
    rev = rev if re.match(r"^r\d+$", rev) else "1"
    imgs, thumbs, fthumbs, cover = {}, {}, {}, ""
    if images:
        try:
            jobs = _image_jobs(F, steps)
            tsz = dict(view=(35.0, 28.0), size=(180, 130), pad=0.1, shadow=False)
            groups = list(F.mfg_by_group) or list(OrderedDict.fromkeys(
                p.group or p.name for p in asm.parts if p.process in mf.FAB_PROCESSES))
            for g in groups:
                p = next((p for p in asm.parts if (p.group or p.name) == g), None)
                if p is not None:            # render frames the bounds itself: no copy needed
                    jobs[("thumb", g)] = dict(items=[(p.mesh, p.color, "normal")], **tsz)
            seen = set()
            for k, e in F.inst.items():
                if e["sku"] in seen:
                    continue
                seen.add(e["sku"])
                ms = [asm.part(n) for n in e["parts"]]
                jobs[("thumb", "buy:" + e["sku"])] = dict(items=[(x.mesh, x.color, "normal") for x in ms], **tsz)
            for s in steps:
                for f in s["fasteners"]:
                    if ("fast", f["spec"]) in jobs:
                        continue
                    fm, col = _fastener_mesh(mf.parse_fastener(f["spec"]))
                    if fm is not None:
                        fm.apply_translation(-fm.bounds.mean(0))
                        jobs[("fast", f["spec"])] = dict(items=[(fm, col, "normal")], view=(35.0, 28.0), size=(120, 90),
                                                         pad=0.1, shadow=False)
            from concurrent.futures import ThreadPoolExecutor

            keys = list(jobs)
            with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as ex:
                pngs = list(ex.map(lambda k: _b64png(render(**jobs[k])), keys))
            res = dict(zip(keys, pngs))
            special = {k[1]: v for k, v in res.items() if k[0] == "step" and isinstance(k[1], str)}
            for k, v in res.items():
                if k[0] == "step" and not isinstance(k[1], str):
                    imgs[k[1]] = v
                elif k[0] == "thumb":
                    thumbs[k[1]] = v
                elif k[0] == "fast":
                    fthumbs[k[1]] = v
                elif k[0] == "cover":
                    cover = v
            for s in steps:
                if s["kind"] in special:
                    imgs[s["index"]] = special[s["kind"]]
        except Exception as exc:  # illustrations are a nicety; never lose the manual over them
            imgs, thumbs, fthumbs, cover = {}, {}, {}, ""
            print(f"manual: illustrations skipped ({exc.__class__.__name__}: {exc})")
    if not cover:
        cover = "data:image/svg+xml;base64," + base64.b64encode(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6"><rect width="10" height="6" fill="#f6f7f9"/></svg>').decode()
    page = _html(F, steps, tools, total, imgs, cover, thumbs, fthumbs, rev)
    with open(os.path.join(outdir, "manual.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    out = {"json": "manual.json", "html": "manual.html", "steps": steps, "tools": tools, "time_min_total": total,
           "title": _robot_title(design), "controller": F.controller}
    with open(os.path.join(outdir, "manual.json"), "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    return out
