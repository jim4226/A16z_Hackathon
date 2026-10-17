"""Component geometry library (purchased parts): actuators, horns, bearings,
fasteners, electronics modules.

Detailed, product-photo geometry built with manifold3d CSG, so every solid is a
robust watertight mesh. Every builder is parametric on the catalog dims (so
entries added at runtime by other modules, e.g. catalog_extra, also build) and
cached: repeated calls return cheap copies.

Frame conventions (component frame, mm):
* actuator(key): origin on the output-shaft axis at the horn mounting face,
  +z out of the shaft; the body extends towards -z; the body's long axis is +x
  and the body centre sits at x = -shaft_off (the shaft is `shaft_off` from the
  body centre towards +x, as in the stub geometry and the SO-101 STS3215 STL;
  use the "body_center" frame rather than re-deriving it). frames:
    "output": identity (where a horn / bracket attaches; rotates with the joint).
              The servo's own horn is included (part suffix "horn") with its outer
              face flush at z = 0; actuator(key, horn=False) leaves it out.
    "horn_seat": where horn(key)'s origin goes (spline top, horn underside)
    "mount_bottom": centre of the body's bottom face (-z end), z pointing out of the bottom
    "idler": idler boss face on the bottom, coaxial with the output, z pointing out of the bottom
    "connector": cable exit point, z pointing along the cable ("connector_b" = 2nd bus socket)
    "body_center": centre of the case, axes as the component frame
    "case_top": case top face on the shaft axis, +z up
  dims = catalog dims + body_center, case_top_z, case_bottom_z, horn_t, overall_h, holes.
* horn(key): origin at the horn underside on the shaft axis, +z out; "face" = outer face.
* module(key): origin at the board's bottom-face centre (centre of the l x w
  footprint, incl. overhanging USB / antenna), +z up through the components,
  long axis +x. frames: "mount" = identity, "connector_*" = cable entry points
  (z pointing out along the cable); "connector_a" always exists.
* bearing(key): origin at the centre of the bearing, axis +z.
* fastener(spec): origin at the underside of the head, shank along -z (flat
  heads: origin at the head top = seated surface; nuts / washers / heat-set
  inserts: origin at the face that seats against the part, body along -z).

Geometry helpers other modules reuse: rounded_box, chamfered_cylinder, tube,
trimesh_from_profile, hex_prism, gear_mesh, offset_stack, text_mesh.
"""

from __future__ import annotations

import copy
import functools
import math
from dataclasses import dataclass, field

import manifold3d as m3d
import numpy as np
import trimesh
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from . import catalog
from .assembly import rot, trans

MF = m3d.Manifold
CS = m3d.CrossSection

# ------------------------------------------------------------------ palette
C_CASE = "#1d1f22"          # servo / enclosure black
C_CASE2 = "#2a2d32"         # label plates, secondary black
C_ALU = "#c4c8cd"           # natural aluminium
C_STEEL = "#b7bcc3"         # zinc / steel
C_CHROME = "#d6dade"
C_BLACKOX = "#2b2c30"       # black-oxide screws
C_GOLD = "#d4aa45"          # ENIG pads, header pins
C_TIN = "#c9ccd1"           # tinned leads, shields
C_COPPER = "#c46b2d"
C_BRASS = "#c9a25b"
C_CREAM = "#ebe2c9"         # JST / Molex housings
C_IC = "#17181b"            # IC packages
C_MARK = "#8e9196"          # laser marking on ICs
C_SILK = "#f3f3ef"          # silkscreen
C_WHITE = "#f1f1ec"
C_HDR = "#141416"           # header plastic
C_CERAMIC = "#b88f5a"       # MLCC body
C_RUBBER = "#1e1f22"

# part suffixes that are cables / plugs / glands hanging off a part (not the body proper)
ACCESSORY_SUFFIXES = ("wire_", "cable", "plug", "gland", "tail", "tabs", "traces", "grommet")   # matched as prefixes


def is_accessory(suffix: str) -> bool:
    """True for cable stubs, plugs, glands, solder tabs: parts that hang off a component's body."""
    return any(suffix.startswith(a) for a in ACCESSORY_SUFFIXES)


@dataclass
class Component:
    key: str
    name: str
    kind: str                                   # actuator | horn | bearing | fastener | electronics | sensor
    parts: list = field(default_factory=list)   # [(suffix, mesh, color, material)]
    frames: dict = field(default_factory=dict)  # name -> 4x4 in component frame
    dims: dict = field(default_factory=dict)
    spec: dict = field(default_factory=dict)

    def mesh(self) -> trimesh.Trimesh:
        return trimesh.util.concatenate([m for _, m, _, _ in self.parts])


# ------------------------------------------------------------------ legacy trimesh primitives (used by arm.py)
def _box(l, w, h, center=(0, 0, 0)):
    m = trimesh.creation.box((l, w, h))
    m.apply_translation(center)
    return m


def _cyl(r, h, z0=0.0, sections=48):
    m = trimesh.creation.cylinder(radius=r, height=h, sections=sections)
    m.apply_translation((0, 0, z0 + h / 2))
    return m


# ------------------------------------------------------------------ manifold core
def _segs(r, lo=12, hi=64):
    n = int(round(2 * math.pi * max(float(r), 0.05) / 0.7))
    n = max(lo, min(hi, n))
    return int(math.ceil(n / 4) * 4)


def _tri(man) -> trimesh.Trimesh:
    if man is None or man.is_empty():
        return trimesh.Trimesh()
    m = man.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(m.tri_verts, dtype=np.int64)
    # keep manifold3d's own vertex sharing: merging coincident vertices of touching shells would make
    # edges non-manifold, while manifold's topology is already a closed 2-manifold
    return trimesh.Trimesh(v, f, process=False)


def _man(mesh: trimesh.Trimesh):
    return MF(m3d.Mesh(vert_properties=np.ascontiguousarray(mesh.vertices, dtype=np.float32),
                       tri_verts=np.ascontiguousarray(mesh.faces, dtype=np.uint32)))


def _xf(man, T):
    return man.transform(np.asarray(T, dtype=float)[:3, :])


def _mv(man, x=0.0, y=0.0, z=0.0):
    return man.translate((float(x), float(y), float(z)))


def _flat(items):
    out = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, (list, tuple)):
            out += _flat(it)
        elif not it.is_empty():
            out.append(it)
    return out


def _U(*items):
    ms = _flat(items)
    if not ms:
        return MF()
    return ms[0] if len(ms) == 1 else MF.batch_boolean(ms, m3d.OpType.Add)


def _D(base, *cutters):
    cs = _flat(cutters)
    if base is None or base.is_empty() or not cs:
        return base
    return MF.batch_boolean([base] + cs, m3d.OpType.Subtract)


def _I(a, b):
    return MF.batch_boolean([a, b], m3d.OpType.Intersect)


def _mcyl(r, h, z0=0.0, segs=None, r2=None, xy=(0.0, 0.0)):
    m = MF.cylinder(float(h), float(r), -1.0 if r2 is None else float(r2), segs or _segs(max(r, r2 or 0.0)))
    return _mv(m, xy[0], xy[1], z0)


def _mbox(l, w, h, c=(0.0, 0.0, 0.0)):
    return _mv(MF.cube((float(l), float(w), float(h)), True), *c)


def _msphere(r, c=(0, 0, 0), segs=12):
    return _mv(MF.sphere(float(r), segs), *c)


def _rr_pts(l, w, r, n=6, chamfer_xp=0.0):
    """Rounded rectangle (CCW, centred). chamfer_xp > 0 replaces the two +x corners by 45 deg chamfers."""
    r = max(0.0, min(r, l / 2 - 1e-3, w / 2 - 1e-3))
    corners = ((l / 2, -w / 2, -90), (l / 2, w / 2, 0), (-l / 2, w / 2, 90), (-l / 2, -w / 2, 180))
    pts = []
    for (cx, cy, a0) in corners:
        if chamfer_xp > 0 and cx > 0:
            c = min(chamfer_xp, w / 2 - 1e-3, l / 2 - 1e-3)
            if cy < 0:
                pts += [(cx - c, cy), (cx, cy + c)]
            else:
                pts += [(cx, cy - c), (cx - c, cy)]
            continue
        if r <= 1e-3:
            pts.append((cx, cy))
            continue
        ox, oy = cx - math.copysign(r, cx), cy - math.copysign(r, cy)
        for k in range(n + 1):
            a = math.radians(a0 + 90.0 * k / n)
            pts.append((ox + r * math.cos(a), oy + r * math.sin(a)))
    return np.array(pts, dtype=float)


def _edge_layers(h, z0, top, bot, steps):
    def arc(f, zb, sgn):
        out = []
        for k in range(steps + 1):
            th = (math.pi / 2) * k / steps
            out.append((zb + sgn * f * (1 - math.cos(th)), f * (1 - math.sin(th))))
        return out
    layers = arc(bot, z0, +1) if bot > 0 else [(z0, 0.0)]
    tl = arc(top, z0 + h, -1) if top > 0 else [(z0 + h, 0.0)]
    return layers + tl[::-1]


def _mrbox(l, w, h, r=1.0, z0=0.0, xy=(0.0, 0.0), top=0.0, bot=0.0, steps=3, n=6, chamfer_xp=0.0):
    """Rounded box (hull of offset layers): vertical edge radius r, top / bottom edge fillets
    `top` / `bot` (steps=1 gives chamfers instead), optional 45 deg chamfered +x corners."""
    top = min(top, h / 2 - 1e-3, l / 2 - 1e-3, w / 2 - 1e-3) if top > 0 else 0.0
    bot = min(bot, h / 2 - 1e-3, l / 2 - 1e-3, w / 2 - 1e-3) if bot > 0 else 0.0
    pts = []
    for z, inset in _edge_layers(h, z0, top, bot, steps):
        p = _rr_pts(l - 2 * inset, w - 2 * inset, max(r - inset, 0.0), n,
                    max(chamfer_xp - inset * 0.414, 0.0) if chamfer_xp else 0.0)
        pts.append(np.column_stack([p + np.asarray(xy, float), np.full(len(p), z)]))
    return MF.hull_points(np.vstack(pts))


def _cs(geom):
    if isinstance(geom, CS):
        return geom
    if geom.geom_type == "Polygon":
        polys = [geom]
    else:
        polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    rings = []
    for p in polys:
        if p.is_empty or p.area < 1e-8:
            continue
        p = orient(p, 1.0)
        rings.append(np.asarray(p.exterior.coords)[:-1])
        rings += [np.asarray(r.coords)[:-1] for r in p.interiors]
    return CS(rings, m3d.FillRule.EvenOdd)


def _mext(geom, h, z0=0.0):
    return _mv(MF.extrude(_cs(geom), float(h)), 0, 0, z0)


def _mrev(pts, segs=48):
    poly = orient(Polygon(pts).buffer(0), 1.0)
    return MF.revolve(_cs(poly), segs)


# extrude (a, b, e) -> world: rotation matrices without reflection
_AX_X = np.array([[0, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])    # a->y, b->z, e->+x
_AX_Y = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])   # a->x, b->z, e->-y


def _ext_x(poly_yz, x0, x1):
    """Extrude a (y, z) profile along +x from x0 to x1."""
    return _xf(_mext(poly_yz, x1 - x0), trans((x0, 0, 0)) @ _AX_X)


def _ext_y(poly_xz, y0, y1):
    """Extrude an (x, z) profile along y from y0 to y1."""
    return _xf(_mext(poly_xz, y1 - y0), trans((0, y1, 0)) @ _AX_Y)


def _zto(axis):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, a)
    s, c = float(np.linalg.norm(v)), float(z @ a)
    if s < 1e-9:
        return np.eye(4) if c > 0 else rot((1, 0, 0), math.pi)
    return rot(v / s, math.atan2(s, c))


def _face(p, normal, up=(0, 0, 1)):
    """4x4 frame at p: +z = normal, +y = `up` projected (so text reads along +x, upright)."""
    z = np.asarray(normal, float)
    z = z / np.linalg.norm(z)
    u = np.asarray(up, float)
    if abs(float(u @ z)) > 0.99:
        u = np.array([0.0, 1.0, 0.0]) if abs(z[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
    y = u - (u @ z) * z
    y /= np.linalg.norm(y)
    x = np.cross(y, z)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2], T[:3, 3] = x, y, z, p
    return T


def _hole(p, normal, r, depth, segs=None):
    """Cylindrical cutter entering a surface at p along -normal."""
    return _xf(_mcyl(r, depth + 0.4, -depth, segs or _segs(r, 10, 24)), _face(p, normal))


def _capsules(points, r, segs=10):
    """Swept round wire through points (union of capsules)."""
    pts = [np.asarray(p, float) for p in points]
    out = []
    for a, b in zip(pts, pts[1:]):
        out.append(MF.batch_hull([_msphere(r, a, segs), _msphere(r, b, segs)]))
    return _U(out)


# ------------------------------------------------------------------ stroke font (labels, silkscreen, chip marking)
_O = [(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]
_P = [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]
_GLYPHS = {
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "C": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "D": [[(0, 0), (0, 6), (2.5, 6), (4, 4.5), (4, 1.5), (2.5, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 6), (3, 6)], [(2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "J": [[(4, 6), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 2)], [(1.3, 3.3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [_O],
    "P": [_P],
    "Q": [_O, [(2.5, 1.5), (4, 0)]],
    "R": [_P, [(2, 3), (4, 0)]],
    "S": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 4), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": [_O, [(1, 1.5), (3, 4.5)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)], [(1.5, 3), (3, 3)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 3.5), (3, 3.5), (4, 2.5), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "6": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "7": [[(0, 6), (4, 6), (1.2, 0)]],
    "8": [[(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0),
           (4, 1), (4, 2), (3, 3)]],
    "9": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (4, 3)]],
    "-": [[(0.8, 3), (3.2, 3)]],
    "+": [[(2, 1.2), (2, 4.8)], [(0.2, 3), (3.8, 3)]],
    ".": [[(2, 0)]],
    ":": [[(2, 1)], [(2, 4.5)]],
    "/": [[(0.3, 0), (3.7, 6)]],
    "%": [[(0, 0), (4, 6)], [(0.6, 5.2)], [(3.4, 0.8)]],
    "(": [[(3, 6), (2, 4.5), (2, 1.5), (3, 0)]],
    ")": [[(1, 6), (2, 4.5), (2, 1.5), (1, 0)]],
    "~": [[(0, 3), (1, 4), (3, 2), (4, 3)]],
}


@functools.lru_cache(maxsize=512)
def _text_geom(text: str, height: float, weight: float = 0.15):
    """Shapely geometry of `text` (stroke font), centred on the origin; returns (geom, width)."""
    s = height / 6.0
    r = weight * height / 2.0
    shapes, x = [], 0.0
    for ch in text.upper():
        for stroke in _GLYPHS.get(ch, []):
            pts = [((x + px) * s, py * s) for px, py in stroke]
            shapes.append(Point(pts[0]).buffer(r * 1.15, quad_segs=3) if len(pts) == 1
                          else LineString(pts).buffer(r, quad_segs=2))
        x += 6.0
    width = max((x - 2.0) * s, 0.0)
    if not shapes:
        return Polygon(), 0.0
    g = unary_union(shapes).simplify(r * 0.12)
    return affinity.translate(g, -width / 2.0, -height / 2.0), width


def _mtext(text, height, relief=0.05, weight=0.15, max_width=None):
    if max_width is not None:
        _, w = _text_geom(text, height, weight)
        if w > max_width > 0:
            height *= max_width / w
    g, _ = _text_geom(text, round(float(height), 4), weight)
    if g.is_empty:
        return MF()
    return _mext(g, relief)


def text_mesh(text: str, height: float, relief: float = 0.1, weight: float = 0.15) -> trimesh.Trimesh:
    """Raised stroke-font text in the XY plane, centred on the origin, z in [0, relief]."""
    return _tri(_mtext(text, height, relief, weight))


# ------------------------------------------------------------------ public geometry helpers
def rounded_box(l, w, h, r=2.0, chamfer=None, center=(0.0, 0.0, 0.0), fillet=None) -> trimesh.Trimesh:
    """l x w x h box centred at `center`: vertical edges rounded (radius r), small 45 deg chamfers on the
    top / bottom edges (default min(0.6, 15% of the smallest side)); fillet=f rounds them instead."""
    if fillet:
        top = bot = float(fillet)
        steps = 3
    else:
        c = min(0.6, 0.15 * min(l, w, h)) if chamfer is None else float(chamfer)
        top = bot = c
        steps = 1
    return _tri(_mrbox(l, w, h, r, center[2] - h / 2.0, (center[0], center[1]), top, bot, steps))


def chamfered_cylinder(r, h, chamfer=0.5, z0=0.0, sections=48, bottom_chamfer=None) -> trimesh.Trimesh:
    """Cylinder z in [z0, z0 + h] with 45 deg edge chamfers."""
    return _tri(_mv(_mchamcyl(r, h, chamfer, sections, bottom_chamfer), 0, 0, z0))


def _mchamcyl(r, h, chamfer=0.5, segs=48, bottom_chamfer=None):
    ct = max(0.0, min(chamfer, r * 0.5, h * 0.45))
    cb = ct if bottom_chamfer is None else max(0.0, min(bottom_chamfer, r * 0.5, h * 0.45))
    pts = [(0, 0), (r - cb, 0), (r, cb), (r, h - ct), (r - ct, h), (0, h)]
    return _mrev(pts, segs)


def tube(r_out, r_in, h, z0=0.0, sections=48, chamfer=0.0) -> trimesh.Trimesh:
    """Tube (ring) z in [z0, z0 + h], optional chamfer on all four circular edges."""
    return _tri(_mv(_mtube(r_out, r_in, h, sections, chamfer), 0, 0, z0))


def _mtube(r_out, r_in, h, segs=48, chamfer=0.0):
    c = max(0.0, min(chamfer, (r_out - r_in) * 0.4, h * 0.4))
    pts = [(r_in + c, 0), (r_out - c, 0), (r_out, c), (r_out, h - c), (r_out - c, h), (r_in + c, h), (r_in, h - c),
           (r_in, c)]
    return _mrev(pts, segs)


def _hex_pts(af):
    R = af / math.sqrt(3.0)
    return [(R * math.cos(math.radians(60 * k)), R * math.sin(math.radians(60 * k))) for k in range(6)]


def _mhex(af, h, z0=0.0, chamfer=0.0):
    """Hex prism across-flats af (flats parallel to x), optional 30 deg corner chamfer on both ends (nut style)."""
    m = _mext(Polygon(_hex_pts(af)), h, z0)
    if chamfer > 0:
        e = af / math.sqrt(3.0)
        rc = af / 2.0 * 0.96
        t = math.tan(math.radians(30))
        prof = [(0, z0), (rc, z0), (e + 0.5, z0 + (e + 0.5 - rc) * t), (e + 0.5, z0 + h - (e + 0.5 - rc) * t),
                (rc, z0 + h), (0, z0 + h)]
        m = _I(m, _mrev(prof, 48))
    return m


def hex_prism(af, h, z0=0.0, chamfer=0.0, hole=0.0) -> trimesh.Trimesh:
    """Hexagonal prism (across-flats `af`, z in [z0, z0 + h]); chamfer>0 adds nut-style corner chamfers."""
    m = _mhex(af, h, z0, chamfer)
    if hole > 0:
        m = _D(m, _mcyl(hole / 2.0, h + 2, z0 - 1))
    return _tri(m)


def _offset_ring(ring, d):
    """Miter-offset a closed ring by d along its left normals (= into the material for oriented polygons)."""
    p = np.asarray(ring, float)
    e1 = p - np.roll(p, 1, axis=0)
    e2 = np.roll(p, -1, axis=0) - p
    e1 /= np.linalg.norm(e1, axis=1, keepdims=True) + 1e-12
    e2 /= np.linalg.norm(e2, axis=1, keepdims=True) + 1e-12
    n1 = np.column_stack([-e1[:, 1], e1[:, 0]])
    n2 = np.column_stack([-e2[:, 1], e2[:, 0]])
    denom = np.maximum(1.0 + np.sum(n1 * n2, axis=1), 0.3)
    return p + d * (n1 + n2) / denom[:, None]


def _clean_ring(coords, tol=1e-4):
    p = np.asarray(coords, float)[:-1]
    keep = [0]
    for i in range(1, len(p)):
        if np.linalg.norm(p[i] - p[keep[-1]]) > tol:
            keep.append(i)
    p = p[keep]
    if len(p) > 1 and np.linalg.norm(p[0] - p[-1]) <= tol:
        p = p[:-1]
    return p


def _loft(poly, layers):
    """Loft a polygon through [(z, inset)] layers (vertex-miter offsets, same topology).
    Returns a Manifold or None when an offset ring self-intersects."""
    poly = orient(poly, 1.0)
    rings = [_clean_ring(poly.exterior.coords)] + [_clean_ring(r.coords) for r in poly.interiors]
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    for inset in sorted({i for _, i in layers if i > 0}):
        off = [_offset_ring(r, inset) for r in rings]
        test = Polygon(off[0], off[1:])
        if not test.is_valid or test.area <= 0 or test.exterior.is_ccw is False:
            return None
        ref = poly.buffer(-inset, join_style=2, mitre_limit=3.0)
        if ref.is_empty or abs(test.area - ref.area) > 0.03 * ref.area + 0.05:
            return None
    counts = [len(r) for r in rings]
    N = sum(counts)
    starts = np.cumsum([0] + counts[:-1])
    V, F = [], []
    for z, inset in layers:
        for r in rings:
            q = _offset_ring(r, inset) if inset else r
            V.append(np.column_stack([q, np.full(len(q), z)]))
    V = np.vstack(V)
    for li in range(len(layers) - 1):
        a0, b0 = li * N, (li + 1) * N
        for s, n in zip(starts, counts):
            i = np.arange(n)
            j = (i + 1) % n
            A, B, C, D = a0 + s + i, a0 + s + j, b0 + s + j, b0 + s + i
            F.append(np.column_stack([A, B, C]))
            F.append(np.column_stack([A, C, D]))
    for li, flip in ((0, True), (len(layers) - 1, False)):
        polys2d = [V[li * N + s: li * N + s + n, :2] for s, n in zip(starts, counts)]
        tris = np.asarray(m3d.triangulate(polys2d), dtype=np.int64) + li * N
        F.append(tris[:, ::-1] if flip else tris)
    mesh = trimesh.Trimesh(V, np.vstack(F), process=False)
    try:
        man = _man(mesh)
    except Exception:
        return None
    if man.is_empty() or man.status() != m3d.Error.NoError:
        return None
    return man


def _profile_man(polygon, thickness, chamfer=0.4, fillet=0.0):
    geoms = [polygon] if polygon.geom_type == "Polygon" else [g for g in polygon.geoms if g.geom_type == "Polygon"]
    out = []
    for g in geoms:
        if g.area < 1e-6:
            continue
        g = g.buffer(0)
        if g.geom_type != "Polygon":
            out.append(_profile_man(g, thickness, chamfer, fillet))
            continue
        c = max(0.0, min(fillet or chamfer, thickness * 0.45))
        man = None
        if c > 1e-4:
            steps = 3 if fillet else 1
            layers = _edge_layers(thickness, 0.0, c, c, steps)
            man = _loft(g, layers)
            if man is None:   # complex outline: Minkowski with a double cone (rounds vertical edges too)
                try:
                    core = _mext(g.buffer(-c, join_style=2), thickness - 2 * c, c)
                    tip = MF.hull_points([(0, 0, -c), (0, 0, c)] + [(c * math.cos(a), c * math.sin(a), 0)
                                                                     for a in np.linspace(0, 2 * math.pi, 9)[:-1]])
                    man = core.minkowski_sum(tip)
                except Exception:
                    man = None
        out.append(man if man is not None and not man.is_empty() else _mext(g, thickness))
    return _U(out)


def trimesh_from_profile(polygon, thickness, chamfer=0.4, fillet=0.0) -> trimesh.Trimesh:
    """2.5D extrusion of a shapely (Multi)Polygon (holes allowed): x = u, y = v, z in [0, thickness],
    with 45 deg chamfers (or rounded fillets) on the top and bottom edges. Watertight."""
    return _tri(_profile_man(polygon, float(thickness), chamfer, fillet))


def gear_mesh(profile_polygon, thickness, chamfer=0.3, bore=0.0, z0=0.0) -> trimesh.Trimesh:
    """Gear / sprocket from its 2D tooth profile: chamfered faces, optional round bore."""
    poly = profile_polygon
    if bore > 0:
        poly = poly.difference(Point(0, 0).buffer(bore / 2.0, quad_segs=12))
    m = _profile_man(poly, float(thickness), chamfer)
    return _tri(_mv(m, 0, 0, z0))


def offset_stack(polygon, layers) -> trimesh.Trimesh:
    """Fillet-ish solids from layered offsets: `layers` = [(z, inset_mm), ...] bottom to top; the outline
    is shrunk by `inset` at height z and the layers are lofted (convex outlines fall back to a hull)."""
    poly = polygon if polygon.geom_type == "Polygon" else max(polygon.geoms, key=lambda g: g.area)
    man = _loft(poly, [(float(z), float(i)) for z, i in layers])
    if man is None:
        pts = []
        for z, inset in layers:
            q = poly.buffer(-inset, join_style=2) if inset else poly
            if not q.is_empty:
                xy = np.asarray(q.convex_hull.exterior.coords)[:-1]
                pts.append(np.column_stack([xy, np.full(len(xy), z)]))
        man = MF.hull_points(np.vstack(pts))
    return _tri(man)


# ------------------------------------------------------------------ part assembly helpers
class _PB:
    """Collects geometry per part suffix; each suffix becomes one watertight mesh with one colour."""

    def __init__(self):
        self.items = {}

    def add(self, suffix, geom, color, material="ABS"):
        if geom is None:
            return
        if isinstance(geom, MF) and geom.is_empty():
            return
        e = self.items.setdefault(suffix, {"g": [], "color": color, "mat": material})
        e["g"].append(geom)

    def put(self, items, T):
        for s, g, c, m in items:
            self.add(s, _xf(g, T), c, m)

    def parts(self):
        out = []
        for suffix, e in self.items.items():
            mans = [g for g in e["g"] if isinstance(g, MF)]
            tris = [g for g in e["g"] if isinstance(g, trimesh.Trimesh) and len(g.faces)]
            meshes = ([_tri(_U(mans))] if mans else []) + tris
            meshes = [m for m in meshes if len(m.faces)]
            if not meshes:
                continue
            mesh = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
            out.append((suffix, mesh, e["color"], e["mat"]))
        return out


def _copy_component(c: Component) -> Component:
    return Component(c.key, c.name, c.kind, [(s, m.copy(), col, mat) for s, m, col, mat in c.parts],
                     {k: np.array(v, dtype=float) for k, v in c.frames.items()}, copy.deepcopy(c.dims),
                     copy.deepcopy(c.spec))


def _place_xy(x, y, z=0.0, deg=0.0):
    return trans((x, y, z)) @ rot((0, 0, 1), math.radians(deg))


def _pan_screw(dk, k, cross=True, segs=24):
    """Pan / cheese head Phillips screw head, underside at z=0."""
    r = dk / 2.0
    head = _mrev([(0, 0), (r, 0), (r, k * 0.55), (r * 0.88, k * 0.9), (r * 0.55, k), (0, k)], segs)
    if cross:
        a, b = dk * 0.56, dk * 0.13
        head = _D(head, _mbox(a, b, k, (0, 0, k)), _mbox(b, a, k, (0, 0, k)))
    return head


def _spline(n, r_out, h, z0=0.0, depth=0.35):
    pts = []
    for i in range(2 * n):
        a = math.pi * i / n
        rr = r_out if i % 2 == 0 else r_out - depth
        pts.append((rr * math.cos(a), rr * math.sin(a)))
    return _mext(Polygon(pts), h, z0)


def _print(pb, suffix, text, height, T, color, relief=0.05, weight=0.15, max_width=None, material="PC"):
    m = _mtext(text, height, relief, weight, max_width)
    if not m.is_empty():
        pb.add(suffix, _xf(m, T), color, material)


# ------------------------------------------------------------------ horns (seat frame: z = 0 at the underside)
def _horn_style(key):
    spec = catalog.ACTUATORS[key]
    fam = spec.get("family", "")
    d = spec["dims"]
    hd = float(d.get("horn_d", 20.0))
    if fam == "smart_bus":
        return dict(style="disc", R=hd / 2, t=3.2, holes=4, pcd=hd * 0.64, hole_d=2.3, color=C_ALU, mat="Al6061",
                    screw=C_STEEL)
    if fam == "dynamixel":
        metal = spec.get("torque_nm", 0) >= 2.0
        return dict(style="robotis", R=hd / 2, t=2.6, holes=8, pcd=hd * 0.8, hole_d=2.1,
                    color=C_ALU if metal else "#25272b", mat="Al6061" if metal else "PA12", screw=C_BLACKOX)
    small = d["w"] < 15
    if key.startswith("ds"):
        return dict(style="metal_disc", R=hd / 2, t=2.6, hub_h=1.8, hub_r=hd * 0.2, holes=4, pcd=hd * 0.56,
                    hole_d=2.6, color=C_ALU, mat="Al6061", screw=C_STEEL)
    return dict(style="hobby", R=hd / 2, t=1.5 if small else 2.0, hub_h=2.1 if small else 2.8,
                hub_r=2.9 if small else 4.0, holes=6, pcd=hd * 0.6, hole_d=1.2 if small else 1.6,
                color=C_WHITE, mat="PA12", screw=C_STEEL)


def _horn_items(key):
    """-> (items [(suffix, MF, color, mat)], total thickness) in the seat frame."""
    st = _horn_style(key)
    R, t = st["R"], st["t"]
    items = []
    if st["style"] == "disc":            # Feetech aluminium disc: 4-hole pattern, recessed centre screw
        disc = _mrev([(0, 0), (R - 0.3, 0), (R, 0.3), (R, t - 0.55), (R - 0.55, t), (0, t)], 72)
        cut = [_mcyl(3.2, 2.0, t - 1.9, 32), _mcyl(1.6, t + 1, -0.5, 20)]
        for k in range(st["holes"]):
            a = math.pi / 2 * k
            p = (st["pcd"] / 2 * math.cos(a), st["pcd"] / 2 * math.sin(a))
            cut += [_mcyl(st["hole_d"] / 2, t + 1, -0.5, 18, xy=p), _mcyl(st["hole_d"] / 2 + 0.35, 0.36, t - 0.35, 18,
                                                                         r2=st["hole_d"] / 2 + 0.7, xy=p)]
        ring_r = (st["pcd"] / 2 + R) / 2 + 0.6
        cut.append(_D(_mcyl(ring_r + 0.22, 0.25, t - 0.2, 72), _mcyl(ring_r - 0.22, 0.6, t - 0.4, 72)))
        items.append(("horn", _D(disc, cut), st["color"], st["mat"]))
        items.append(("horn_screw", _mv(_pan_screw(5.6, 1.7), 0, 0, t - 1.9), st["screw"], "Steel"))
        return items, t
    if st["style"] == "robotis":         # ROBOTIS horn: 8 holes, centre screw, index dimple
        disc = _mrev([(0, 0), (R - 0.25, 0), (R, 0.25), (R, t - 0.4), (R - 0.4, t), (0, t)], 72)
        cut = [_mcyl(2.3, 1.4, t - 1.3, 28), _mcyl(1.3, t + 1, -0.5, 16), _mcyl(R * 0.55, 0.25, t - 0.2, 64)]
        for k in range(st["holes"]):
            a = math.radians(22.5 + 45 * k)
            cut.append(_mcyl(st["hole_d"] / 2, t + 1, -0.5, 16, xy=(st["pcd"] / 2 * math.cos(a), st["pcd"] / 2 * math.sin(a))))
        cut.append(_mcyl(0.45, 0.5, t - 0.3, 10, xy=(R * 0.42, 0)))
        items.append(("horn", _D(disc, cut), st["color"], st["mat"]))
        sc = _D(_mcyl(2.0, 1.0, 0, 24), _mxsock(1.5, 0.6, 1.0))
        items.append(("horn_screw", _mv(sc, 0, 0, t - 1.3), st["screw"], "Steel"))
        return items, t
    hub_h, hub_r = st["hub_h"], st["hub_r"]
    tt = hub_h + t
    if st["style"] == "metal_disc":      # DS3218 aluminium 25T disc horn with clamp hub
        body = _U(_mrev([(0, 0), (hub_r, 0), (hub_r, hub_h), (R - 0.3, hub_h), (R, hub_h + 0.3), (R, tt - 0.4),
                         (R - 0.4, tt), (0, tt)], 72))
        cut = [_mcyl(2.1, 1.3, tt - 1.2, 28), _mcyl(1.5, tt + 1, -0.5, 16)]
        for k in range(st["holes"]):
            a = math.pi / 4 + math.pi / 2 * k
            cut.append(_mcyl(st["hole_d"] / 2, tt + 1, -0.5, 16, xy=(st["pcd"] / 2 * math.cos(a), st["pcd"] / 2 * math.sin(a))))
        for k in range(4):
            a = math.pi / 2 * k
            cut.append(_mcyl(0.8, tt + 1, -0.5, 12, xy=(R * 0.8 * math.cos(a), R * 0.8 * math.sin(a))))
        items.append(("horn", _D(body, cut), st["color"], st["mat"]))
        items.append(("horn_screw", _mv(_pan_screw(3.8, 1.1), 0, 0, tt - 1.2), st["screw"], "Steel"))
        return items, tt
    # classic injection-moulded round horn: hub + disc, rings of holes along 6 spokes
    body = _mrev([(0, 0), (hub_r, 0), (hub_r, hub_h), (R - 0.25, hub_h), (R, hub_h + 0.25), (R, tt - 0.3),
                  (R - 0.3, tt), (0, tt)], 72)
    cut = [_mcyl(1.9, 1.4, tt - 1.2, 24), _mcyl(1.1, tt + 1, -0.5, 14)]
    for k in range(st["holes"]):
        a = math.radians(30 + 60 * k)
        for rr in np.arange(R - 1.6, hub_r + 1.3, -2.1):
            cut.append(_mcyl(st["hole_d"] / 2, t + 1, hub_h - 0.5, 10, xy=(rr * math.cos(a), rr * math.sin(a))))
    items.append(("horn", _D(body, cut), st["color"], st["mat"]))
    items.append(("horn_screw", _mv(_pan_screw(3.4, 1.0), 0, 0, tt - 1.15), st["screw"], "Steel"))
    return items, tt


def _mxsock(af, depth, top):
    """Hex socket cutter (across-flats af, depth below `top`) with a conical tip."""
    hexm = _mhex(af, depth + 1.0, top - depth)
    cone = _mcyl(0.05, af * 0.35, top - depth - af * 0.35, 6, r2=af / math.sqrt(3))
    return _U(hexm, cone)


@functools.lru_cache(maxsize=64)
def _horn_cached(key):
    spec = catalog.get(key)
    items, t = _horn_items(key)
    pb = _PB()
    for s, g, c, m in items:
        pb.add(s, g, c, m)
    st = _horn_style(key)
    frames = {"seat": np.eye(4), "face": trans((0, 0, t))}
    holes = []
    n = st["holes"]
    for k in range(n):
        a = (math.pi / 2 * k) if st["style"] == "disc" else (math.radians(22.5 + 45 * k) if st["style"] == "robotis"
                                                              else math.pi / 4 + 2 * math.pi / n * k)
        holes.append([round(st["pcd"] / 2 * math.cos(a), 3), round(st["pcd"] / 2 * math.sin(a), 3), t, st["hole_d"]])
    return Component(f"{key}_horn", f"{spec['name'].split('(')[0].strip()} output horn", "horn", pb.parts(), frames,
                     {"d": 2 * st["R"], "t": t, "pcd": st["pcd"], "holes": holes}, {"key": f"{key}_horn", "for": key})


def horn(key: str) -> Component:
    """The output horn delivered with actuator `key`, in its seat frame (underside z = 0, "face" on top)."""
    return _copy_component(_horn_cached(key))


# ------------------------------------------------------------------ actuators
def _seam(l, w, r, z, xy, depth=0.35, width=0.45, chamfer_xp=0.0):
    outer = _mrbox(l + 4, w + 4, width, r + 2, z - width / 2, xy, chamfer_xp=chamfer_xp + 2 if chamfer_xp else 0.0)
    inner = _mrbox(l - 2 * depth, w - 2 * depth, width + 0.2, max(r - depth, 0.1), z - width / 2 - 0.1, xy,
                   chamfer_xp=max(chamfer_xp - depth * 0.4, 0.0) if chamfer_xp else 0.0)
    return _D(outer, inner)


def _bus_socket(pitch=2.5, n=3, w=None, h=4.6, depth=5.0):
    """Molex 5264 / JST EH style wire-to-board socket: local frame opening faces -x (front at x=0),
    body to +x, width along y, bottom at z=0. -> (housing, pins)."""
    w = w or n * pitch + 1.4
    body = _mbox(depth, w, h, (depth / 2, 0, h / 2))
    pocket = _mbox(depth * 0.8, n * pitch + 0.1, h - 1.3, (depth * 0.4 - 0.05, 0, h / 2 - 0.15))
    key_slot = _mbox(1.0, 1.2, 0.8, (0.45, 0, h - 0.35))
    housing = _D(body, pocket, key_slot)
    pins = _U([_mbox(depth * 0.75, 0.64, 0.64, (depth * 0.45, (i - (n - 1) / 2) * pitch, h / 2 - 0.15)) for i in range(n)])
    return housing, pins


def _act_frames(cx, z_top, z_bot, ht, idler_z, conn, conn_b=None):
    f = {"output": np.eye(4), "horn_seat": trans((0, 0, -ht)),
         "mount_bottom": trans((cx, 0, z_bot)) @ rot((1, 0, 0), math.pi),
         "idler": trans((0, 0, idler_z)) @ rot((1, 0, 0), math.pi),
         "connector": conn, "body_center": trans((cx, 0, (z_top + z_bot) / 2)),
         "case_top": trans((0, 0, z_top))}
    if conn_b is not None:
        f["connector_b"] = conn_b
    return f


def _act_sts(key, spec, with_horn):
    """Feetech STS-style smart servo (SO-101 look): chamfered horn end, raised top lid, idler disc below,
    two 3-pin bus sockets on the bottom face."""
    d = spec["dims"]
    L, W, H, so = float(d["l"]), float(d["w"]), float(d["h"]), float(d["shaft_off"])
    s = W / 24.7
    cx = -so
    items, ht = _horn_items(key)
    z_top = -(ht + 1.25 * s)
    z_bot = z_top - H
    xe_m = cx - L / 2
    chx = 5.0 * s
    pb = _PB()
    case = _mrbox(L, W, H, 1.4 * s, z_bot, (cx, 0), top=1.0 * s, bot=0.8 * s, steps=3, chamfer_xp=chx)
    cut = [_seam(L, W, 1.4 * s, z_top - 6.5 * s, (cx, 0), chamfer_xp=chx),
           _seam(L, W, 1.4 * s, z_bot + 6.0 * s, (cx, 0), chamfer_xp=chx)]
    # top: pocket around the horn, lid strip with screw holes on the far part
    cut.append(_mcyl(12.6 * s, 0.6 * s, z_top - 0.6 * s, 72))
    lid_x0 = 9.8 * s                    # lid starts this far from the shaft axis (towards -x)
    lid_l = (0 - lid_x0) - (xe_m + 1.3 * s)
    lid = _mrbox(lid_l, W - 4.2 * s, 1.1 * s, 0.6 * s, z_top - 0.01, (-lid_x0 - lid_l / 2, 0), top=0.35 * s, steps=1)
    lid = _D(lid, _mcyl(12.9 * s, 3 * s, z_top - 1, 72))
    for sy in (-1, 1):
        for xx in (-lid_x0 - 3.0 * s, xe_m + 4.0 * s):
            cut.append(_mcyl(0.95 * s, 6 * s, z_top - 4.5 * s, 16, xy=(xx, sy * 8.6 * s)))
    # corner case screws at the far end (heads sit proud of the top)
    screws_top = [(xe_m + 2.6 * s, sy * (W / 2 - 2.6 * s)) for sy in (-1, 1)]
    for p in screws_top:
        cut.append(_mcyl(2.0 * s, 1.2 * s, z_top - 1.0 * s, 24, xy=p))
    # sides: threaded mounting holes (2 per long face, 2 on the far end face), label recess on -y
    zc = (z_top + z_bot) / 2
    holes_side = []
    for sy in (-1, 1):
        for xx in (cx - 14.0 * s, cx + 14.0 * s):
            if sy < 0:
                continue
            p = (xx, sy * W / 2, zc)
            cut.append(_hole(p, (0, sy, 0), 1.0 * s, 4.0 * s))
            holes_side.append(list(map(float, p)))
    for yy in (-7.5 * s, 7.5 * s):
        p = (xe_m, yy, zc + 4.0 * s)
        cut.append(_hole(p, (-1, 0, 0), 1.0 * s, 4.0 * s))
        holes_side.append(list(map(float, p)))
    lab_c = (cx - 1.0 * s, -W / 2, zc + 0.4 * s)
    lab_w, lab_h = 27.0 * s, 14.0 * s
    cut.append(_mbox(lab_w, 0.6 * s, lab_h, lab_c))
    # bottom: idler disc pocket, bus sockets, case-screw counterbores
    cut.append(_mcyl(11.4 * s, 0.7 * s, z_bot - 0.1, 72))
    sock_x = cx - 6.0 * s
    for sy in (-1, 1):
        cut.append(_mbox(9.0 * s, 8.6 * s, 5.4 * s, (sock_x, sy * 5.0 * s, z_bot + 2.6 * s)))
    cbs = [(xe_m + 2.6 * s, sy * (W / 2 - 2.6 * s)) for sy in (-1, 1)] + [(cx + 4.0 * s, sy * (W / 2 - 2.4 * s)) for sy in (-1, 1)]
    for p in cbs:
        cut.append(_mcyl(1.9 * s, 1.6 * s, z_bot - 0.1, 20, xy=p))
    case = _D(case, cut)
    pb.add("case", _U(case, lid), C_CASE, "ABS")
    # output boss + spline visible under the horn
    pb.add("case", _mrev([(5.0 * s, z_top - 0.7), (8.0 * s, z_top - 0.7), (8.0 * s, z_top + 0.25 * s),
                          (7.6 * s, z_top + 0.55 * s), (5.0 * s, z_top + 0.55 * s)], 64), C_CASE, "ABS")
    pb.add("spline", _spline(25, 2.95 * s, -ht - (z_top + 0.3) + 0.2, z_top + 0.3), C_STEEL, "Steel")
    # screws: top corner heads, bottom counterbores
    for p in screws_top:
        pb.add("screws", _mv(_pan_screw(3.4 * s, 1.5 * s), p[0], p[1], z_top - 1.0 * s), C_STEEL, "Steel")
    for p in cbs:
        pb.add("screws", _xf(_pan_screw(3.0 * s, 1.1 * s), trans((p[0], p[1], z_bot + 1.5 * s)) @ rot((1, 0, 0), math.pi)),
               C_STEEL, "Steel")
    # idler disc on the bottom (coaxial with the output), 4 holes + centre
    t_id = 2.0 * s
    idler = _mrev([(0, 0), (10.2 * s, 0), (10.2 * s, t_id - 0.4 * s), (9.8 * s, t_id), (0, t_id)], 64)
    idc = [_mcyl(1.5 * s, t_id + 1, -0.5, 16), _mcyl(3.6 * s, 0.4 * s, -0.1, 32)]
    for k in range(4):
        a = math.pi / 4 + math.pi / 2 * k
        idc.append(_mcyl(1.0 * s, t_id + 1, -0.5, 14, xy=(7.0 * s * math.cos(a), 7.0 * s * math.sin(a))))
    idler = _xf(_D(idler, idc), trans((0, 0, z_bot + 0.6 * s)) @ rot((1, 0, 0), math.pi))
    pb.add("idler", idler, "#2c2f34", "PA12")
    idler_z = z_bot + 0.6 * s - t_id
    # bus sockets on the bottom, openings facing -z
    hs, pins = _bus_socket(2.5 * s, 3, 8.0 * s, 4.6 * s, 4.8 * s)
    M = np.array([[0, 0, -1, 0], [0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1.0]])   # socket depth -> +z, height -> -x
    for sy in (-1, 1):
        T = trans((sock_x + 4.6 * s / 2, sy * 5.0 * s, z_bot + 0.15 * s)) @ M
        pb.add("connectors", _xf(hs, T), C_CREAM, "PA12")
        pb.add("pins", _xf(pins, T), C_GOLD, "Copper")
    # label + printing
    plate = _mbox(lab_w - 0.5 * s, 0.25, lab_h - 0.5 * s, (lab_c[0], -W / 2 + 0.22, lab_c[2]))
    pb.add("label", plate, "#141518", "PC")
    F = _face((lab_c[0], -W / 2 + 0.09, lab_c[2]), (0, -1, 0))
    _print(pb, "print", "FEETECH", 2.0 * s, F @ trans((0, 3.6 * s, 0)), "#e4e4e0", max_width=lab_w - 3)
    model = key.upper().split("_")[0]
    _print(pb, "print", model, 2.9 * s, F @ trans((0, 0.0, 0)), "#e4e4e0", weight=0.16, max_width=lab_w - 3)
    _print(pb, "print", f"{spec.get('nominal_v', 12):g}V  1:345", 1.3 * s, F @ trans((0, -3.6 * s, 0)), "#a9acb0",
           max_width=lab_w - 4)
    if with_horn:
        pb.put(items, trans((0, 0, -ht)))
    conn = trans((sock_x, -5.0 * s, z_bot)) @ rot((1, 0, 0), math.pi)
    conn_b = trans((sock_x, 5.0 * s, z_bot)) @ rot((1, 0, 0), math.pi)
    frames = _act_frames(cx, z_top, z_bot, ht, idler_z, conn, conn_b)
    holes = {"top": [[float(-lid_x0 - 3.0 * s), sy * 8.6 * s, z_top + 1.1 * s] for sy in (-1, 1)]
             + [[float(xe_m + 4.0 * s), sy * 8.6 * s, z_top + 1.1 * s] for sy in (-1, 1)],
             "side": holes_side, "idler": [[7.0 * s * math.cos(math.pi / 4 + math.pi / 2 * k),
                                            7.0 * s * math.sin(math.pi / 4 + math.pi / 2 * k), idler_z] for k in range(4)]}
    return pb, frames, dict(cx=cx, z_top=z_top, z_bot=z_bot, ht=ht, holes=holes)


def _act_dxl(key, spec, with_horn):
    """ROBOTIS DYNAMIXEL X-series: front / middle / back case, horn on top, idler below,
    tapped mounting holes on every face, two JST sockets on the back end."""
    d = spec["dims"]
    L, W, H, so = float(d["l"]), float(d["w"]), float(d["h"]), float(d["shaft_off"])
    s = W / 28.5
    cx = -so
    items, ht = _horn_items(key)
    metal = spec.get("torque_nm", 0) >= 2.0
    z_top = -(ht + 0.95 * s)
    z_bot = z_top - H
    hf, hb = 7.0 * s, 5.5 * s
    r = 1.8 * s
    pb = _PB()
    front = _mrbox(L, W, hf, r, z_top - hf, (cx, 0), top=0.9 * s, steps=1)
    back = _mrbox(L, W, hb, r, z_bot, (cx, 0), bot=0.9 * s, steps=1)
    mid = _mrbox(L - 0.5 * s, W - 0.5 * s, H - hf - hb + 0.02, r - 0.25 * s, z_bot + hb - 0.01, (cx, 0))
    xe_p, xe_m = cx + L / 2, cx - L / 2
    zc = (z_top + z_bot) / 2
    hx = 17.5 * s
    cut_f, cut_b, cut_m = [], [], []
    holes = {"top": [], "bottom": [], "side": []}
    for sx in (-1, 1):
        for sy in (-1, 1):
            p = (cx + sx * hx, sy * 10.0 * s)
            cut_f.append(_mcyl(1.0 * s, 5 * s, z_top - 4.5 * s, 16, xy=p))
            cut_b.append(_mcyl(1.0 * s, 5 * s, z_bot - 0.5, 16, xy=p))
            holes["top"].append([p[0], p[1], z_top])
            holes["bottom"].append([p[0], p[1], z_bot])
    # +y face: grid of tapped holes; -y face: label recess + 4 holes; -x end: 2 holes
    for xx in (cx - 16 * s, cx - 6 * s, cx + 6 * s, cx + 16 * s):
        for zz in (zc - 6 * s, zc + 6 * s):
            cut_m.append(_hole((xx, W / 2 - 0.25 * s, zz), (0, 1, 0), 1.0 * s, 4 * s))
            holes["side"].append([xx, W / 2, zz])
    for xx in (cx - 19.5 * s, cx + 19.5 * s):
        for zz in (zc - 6 * s, zc + 6 * s):
            cut_m.append(_hole((xx, -W / 2 + 0.25 * s, zz), (0, -1, 0), 1.0 * s, 4 * s))
            holes["side"].append([xx, -W / 2, zz])
    for yy in (-8 * s, 8 * s):
        cut_m.append(_hole((xe_p - 0.25 * s, yy, zc), (1, 0, 0), 1.0 * s, 4 * s))
    lab_c = (cx, -W / 2 + 0.25 * s, zc)
    lab_w, lab_h = 30.0 * s, 13.0 * s
    cut_m.append(_mbox(lab_w, 0.6 * s, lab_h, lab_c))
    # back end (-x): two JST sockets + LED
    for sy in (-1, 1):
        cut_b.append(_mbox(5.2 * s, 8.2 * s, 4.8 * s, (xe_m + 2.4 * s, sy * 6.6 * s, z_bot + 3.4 * s)))
        cut_m.append(_mbox(5.2 * s, 8.2 * s, 4.8 * s, (xe_m + 2.4 * s, sy * 6.6 * s, z_bot + 3.4 * s)))
    # bottom idler boss pocket
    cut_b.append(_mcyl(4.6 * s, 0.6 * s, z_bot - 0.1, 40))
    front = _D(front, cut_f, _mcyl(1.3 * s, 2 * s, z_top - 1.0 * s, 24))
    back = _D(back, cut_b)
    mid = _D(mid, cut_m)
    boss = _D(_mrev([(0, z_top - 0.5), (7.3 * s, z_top - 0.5), (7.3 * s, z_top + 0.3 * s), (7.0 * s, z_top + 0.55 * s),
                     (0, z_top + 0.55 * s)], 64), _mcyl(2.9 * s, 2, z_top, 32))
    idl = _mrev([(4.6 * s, 0), (6.6 * s, 0), (6.6 * s, 0.45 * s), (6.3 * s, 0.7 * s), (4.6 * s, 0.7 * s)], 48)
    shell = _U(front, back, boss, _xf(idl, trans((0, 0, z_bot + 0.05)) @ rot((1, 0, 0), math.pi)))
    if metal:
        pb.add("case", shell, C_CASE, "ABS")
        pb.add("case_mid", mid, "#3a3d43", "Al6061")
    else:
        pb.add("case", _U(shell, mid), C_CASE, "ABS")
    pb.add("spline", _spline(24, 2.75 * s, -ht - (z_top + 0.2) + 0.2, z_top + 0.2), C_STEEL, "Steel")
    # idler bearing face (silver ring) in the bottom pocket
    pb.add("idler", _mv(_mtube(4.3 * s, 1.6 * s, 0.35 * s, 40), 0, 0, z_bot - 0.05 * s), C_STEEL, "Steel")
    idler_z = z_bot - 0.7 * s
    hs, pins = _bus_socket(2.5 * s, 3, 7.8 * s, 4.6 * s, 5.0 * s)
    for sy in (-1, 1):
        T = trans((xe_m + 0.15 * s, sy * 6.6 * s, z_bot + 1.1 * s))
        pb.add("connectors", _xf(hs, T), C_CREAM, "PA12")
        pb.add("pins", _xf(pins, T), C_GOLD, "Copper")
    pb.add("led", _mv(_xf(_mcyl(0.8 * s, 0.5 * s, -0.3 * s, 16), _zto((-1, 0, 0))), xe_m, 0, z_bot + 8.0 * s), "#ff4a3a", "PC")
    plate = _mbox(lab_w - 0.5 * s, 0.25, lab_h - 0.5 * s, (lab_c[0], -W / 2 + 0.2 * s + 0.22, lab_c[2]))
    pb.add("label", plate, "#25272c", "PC")
    F = _face((lab_c[0], -W / 2 + 0.25 * s + 0.09, lab_c[2]), (0, -1, 0))
    _print(pb, "print", "DYNAMIXEL", 3.0 * s, F @ trans((0, 2.6 * s, 0)), "#eeeeea", weight=0.17, max_width=lab_w - 3)
    model = {"xl430_w250": "XL430-W250-T", "xm430_w350": "XM430-W350-T"}.get(key, key.upper().replace("_", "-"))
    _print(pb, "print", model, 1.9 * s, F @ trans((0, -2.0 * s, 0)), "#c9cbcf", max_width=lab_w - 3)
    _print(pb, "print", "ROBOTIS", 1.3 * s, F @ trans((0, -4.7 * s, 0)), "#9ea2a8", max_width=lab_w - 3)
    if with_horn:
        pb.put(items, trans((0, 0, -ht)))
    conn = trans((xe_m, -6.6 * s, z_bot + 3.4 * s)) @ _zto((-1, 0, 0))
    conn_b = trans((xe_m, 6.6 * s, z_bot + 3.4 * s)) @ _zto((-1, 0, 0))
    frames = _act_frames(cx, z_top, z_bot, ht, idler_z, conn, conn_b)
    return pb, frames, dict(cx=cx, z_top=z_top, z_bot=z_bot, ht=ht, holes=holes)


_HOBBY_LOOK = {   # case colour, mid colour (None = same), label plate colour, label lines, spline colour
    "sg90": ("#2f6fdc", None, None, ("SG90", "9G MICRO SERVO"), C_WHITE),
    "mg996r": ("#17181b", None, "#121315", ("MG996R", "TOWERPRO"), C_BRASS),
    "ds3218": ("#17181b", "#3b3e44", "#1f5fbf", ("DS3218", "DSSERVO 20KG"), C_BRASS),
}


def _act_hobby(key, spec, with_horn):
    """Classic PWM hobby servo: three-piece case, mounting ears with gussets, gear tower, spline,
    round horn, label, 3-wire brown/red/orange cable stub."""
    d = spec["dims"]
    L, W, H, so = float(d["l"]), float(d["w"]), float(d["h"]), float(d["shaft_off"])
    cx = -so
    look = _HOBBY_LOOK.get(key, ("#17181b", None, "#121315", (key.upper(), "SERVO"), C_BRASS))
    case_c, mid_c, lab_c_, lines, spl_c = look
    small = W < 15
    items, ht = _horn_items(key)
    tower_h = 3.6 if small else 4.2
    tower_r = min(W / 2 - 0.3, L / 2 - so + 0.1) if small else min(W * 0.36, 7.4)
    z_tt = -(ht + 0.45)
    z_top = z_tt - tower_h
    z_bot = z_top - H
    ear_z = float(d.get("ear_z") or H * 0.68)
    ear_t = float(d.get("ear_t") or 2.5)
    z_e0, z_e1 = z_bot + ear_z, z_bot + ear_z + ear_t
    bc = H * 0.13
    r = 0.9 if small else 1.2
    pb = _PB()
    bottom = _mrbox(L, W, bc, r, z_bot, (cx, 0), bot=0.6, steps=2)
    middle = _mrbox(L - 0.35, W - 0.35, z_e1 - (z_bot + bc), r - 0.15, z_bot + bc, (cx, 0))
    topcap = _mrbox(L, W, z_top - z_e1, r, z_e1, (cx, 0), top=1.0 if small else 1.4, steps=3)
    tower = _mrev([(0, z_top - 0.5), (tower_r, z_top - 0.5), (tower_r, z_tt - 0.5), (tower_r - 0.5, z_tt), (0, z_tt)], 64)
    br, bh = tower_r * 0.5, tower_h * 0.55
    bump = _mrev([(0, z_top - 0.5), (br, z_top - 0.5), (br, z_top + bh - 0.4), (br - 0.4, z_top + bh), (0, z_top + bh)], 40)
    bump = _mv(bump, -tower_r * 1.05, 0, 0)
    top_parts = _D(_U(topcap, tower, bump), _mcyl(1.2, 3, z_tt - 1.0, 16))
    # bottom cover screws
    scr = [(cx + sx * (L / 2 - 2.0), sy * (W / 2 - 2.0)) for sx in (-1, 1) for sy in (-1, 1)]
    bottom = _D(bottom, [_mcyl(1.3, 1.0, z_bot - 0.1, 16, xy=p) for p in scr])
    lab_w, lab_h = L - 4.0, (z_e0 - (z_bot + bc)) - 3.0
    lab_c = (cx, -(W - 0.35) / 2, (z_e0 + z_bot + bc) / 2)
    if mid_c:
        pb.add("case", _U(bottom, top_parts), case_c, "PC")
        pb.add("case_mid", middle, mid_c, "Al6061")
    else:
        pb.add("case", _U(bottom, middle, top_parts), case_c, "PC")
    for p in scr:
        pb.add("screws", _xf(_pan_screw(2.2, 0.8), trans((p[0], p[1], z_bot + 0.9)) @ rot((1, 0, 0), math.pi)), C_STEEL, "Steel")
    # ears with holes (+ slot) and gussets
    ear_l = float(d.get("ear_l") or L + 7.0)
    ear_w = float(d.get("ear_w") or 0.0)
    hole_d = 2.0 if small else 4.4
    E = ear_l / 2 + hole_d / 2 + (1.6 if small else 2.0)
    ears = _mrbox(2 * E, W - 0.2, ear_t, 1.0, z_e0, (cx, 0))
    ecut = []
    ear_holes = []
    for sx in (-1, 1):
        hx = cx + sx * ear_l / 2
        ys = (0.0,) if ear_w <= 0 else (-ear_w / 2, ear_w / 2)
        for yy in ys:
            ecut.append(_mcyl(hole_d / 2, ear_t + 2, z_e0 - 1, 20, xy=(hx, yy)))
            ear_holes.append([hx, yy, z_e0, hole_d])
        slot_w = 1.0 if small else 1.6
        ecut.append(_mbox(E - ear_l / 2 + 1.0, slot_w, ear_t + 2,
                          (cx + sx * (ear_l / 2 + (E - ear_l / 2) / 2 + 0.6), 0, z_e0 + ear_t / 2)))
    ears = _D(ears, ecut)
    g = min(E - L / 2, 4.0) * 0.8
    gus = []
    for sx in (-1, 1):
        wall = cx + sx * L / 2
        tri = Polygon([(wall - sx * 0.3, z_e0 + 0.1), (wall + sx * g, z_e0 + 0.1), (wall - sx * 0.3, z_e0 - g * 1.2)])
        for yy in (-(W / 2 - 1.3), W / 2 - 1.3):
            gus.append(_ext_y(tri, yy - 0.5, yy + 0.5))
    pb.add("ears", _U(ears, gus), mid_c if mid_c else case_c, "Al6061" if mid_c else "PC")
    # spline (visible between tower and horn) + output horn
    pb.add("spline", _spline(25 if not small else 21, 2.95 if not small else 2.4, -ht - z_tt + 0.6, z_tt - 0.3), spl_c,
           "Steel" if spl_c != C_WHITE else "PA12")
    # label (sticker) on the -y face of the middle section
    if lab_c_:
        pb.add("label", _mbox(lab_w, 0.2, lab_h, (lab_c[0], lab_c[1] - 0.08, lab_c[2])), lab_c_, "PC")
    F = _face((lab_c[0], lab_c[1] - 0.18 if lab_c_ else lab_c[1], lab_c[2]), (0, -1, 0))
    _print(pb, "print", lines[0], min(lab_h * 0.36, 4.0), F @ trans((0, lab_h * 0.14, 0)), "#f2f2ee", weight=0.17,
           max_width=lab_w - 2)
    _print(pb, "print", lines[1], min(lab_h * 0.18, 1.8), F @ trans((0, -lab_h * 0.26, 0)), "#d0d2d6",
           max_width=lab_w - 2)
    # cable: grommet + three wires (brown GND, red V+, orange signal)
    wd = 0.85 if small else 1.15
    xe = cx - L / 2
    zc0 = z_bot + bc + wd / 2 + 0.9
    grom = _mrbox(2.6, 3 * wd + 1.4, wd + 1.4, 0.4, zc0 - (wd + 1.4) / 2, (xe - 1.0, 0), top=0.3, bot=0.3, steps=1)
    pb.add("grommet", grom, case_c, "PC")
    run = 20.0 if small else 26.0
    for i, (suf, col) in enumerate((("wire_brown", "#5a3a1f"), ("wire_red", "#c3261f"), ("wire_orange", "#f0901a"))):
        yy = (i - 1) * (wd + 0.04)
        pts = [(xe - 1.5, yy, zc0), (xe - run * 0.4, yy, zc0), (xe - run * 0.75, yy - 0.6, zc0 - 0.8),
               (xe - run, yy - 1.6, zc0 - 2.4)]
        pb.add(suf, _capsules(pts, wd / 2, 10), col, "PC")
    if with_horn:
        pb.put(items, trans((0, 0, -ht)))
    conn = trans((xe - 2.3, 0, zc0)) @ _zto((-1, 0, 0))
    frames = _act_frames(cx, z_top, z_bot, ht, z_bot, conn)
    frames["mount_ears"] = trans((cx, 0, z_e0)) @ rot((1, 0, 0), math.pi)
    frames["cable_end"] = trans((xe - run, -1.6, zc0 - 2.4)) @ _zto((-1, 0, 0))
    holes = {"ears": ear_holes}
    return pb, frames, dict(cx=cx, z_top=z_top, z_bot=z_bot, ht=ht, holes=holes)


@functools.lru_cache(maxsize=64)
def _actuator_cached(key, with_horn):
    spec = catalog.get(key)
    fam = spec.get("family", "")
    if fam == "dynamixel":
        pb, frames, g = _act_dxl(key, spec, with_horn)
    elif fam in ("smart_bus",) or spec.get("interface") == "ttl_half_duplex":
        pb, frames, g = _act_sts(key, spec, with_horn)
    else:
        pb, frames, g = _act_hobby(key, spec, with_horn)
    d = dict(spec["dims"])
    d.update({"body_center": [g["cx"], 0.0, (g["z_top"] + g["z_bot"]) / 2], "case_top_z": g["z_top"],
              "case_bottom_z": g["z_bot"], "horn_t": g["ht"], "overall_h": -g["z_bot"], "holes": g["holes"]})
    return Component(key, spec["name"], "actuator", pb.parts(), frames, d, spec)


def actuator(key: str, horn: bool = True) -> Component:
    """Detailed purchased actuator; `horn=False` leaves the output horn off (frames unchanged)."""
    return _copy_component(_actuator_cached(key, bool(horn)))


# ------------------------------------------------------------------ bearings
_SEAL_COLOR = {"608": "#c8432d"}   # skate bearings: orange-red rubber seals; others black


@functools.lru_cache(maxsize=32)
def _bearing_cached(key):
    b = catalog.BEARINGS[key]
    d, D, B = float(b["d"]), float(b["D"]), float(b["B"])
    pb = _PB()
    frames = {"center": np.eye(4), "top": trans((0, 0, B / 2)), "bottom": trans((0, 0, -B / 2)) @ rot((1, 0, 0), math.pi)}
    if key.startswith("lazy") or (D - d) > 30:
        _lazy_susan(pb, d, D, B)
    else:
        ri, ro = d / 2, D / 2
        span = ro - ri
        wi, wo = 0.30 * span, 0.26 * span
        r2, r1 = ri + wi, ro - wo
        c, c2 = min(0.5, 0.08 * span + 0.15), 0.2
        segs = _segs(ro, 32, 96)
        outer = [(r1, -B / 2 + c2), (r1 + c2, -B / 2), (ro - c, -B / 2), (ro, -B / 2 + c), (ro, B / 2 - c),
                 (ro - c, B / 2), (r1 + c2, B / 2), (r1, B / 2 - c2)]
        inner = [(ri, -B / 2 + c), (ri + c, -B / 2), (r2 - c2, -B / 2), (r2, -B / 2 + c2), (r2, B / 2 - c2),
                 (r2 - c2, B / 2), (ri + c, B / 2), (ri, B / 2 - c)]
        pb.add("outer_race", _mrev(outer, segs), C_CHROME, "Steel")
        pb.add("inner_race", _mrev(inner, _segs(r2, 32, 96)), C_CHROME, "Steel")
        rec, st = 0.3, 0.55
        mid = (r1 + r2) / 2
        seal = [(r2 + 0.02, 0), (r1 - 0.02, 0), (r1 - 0.02, st), (mid + 0.35, st), (mid + 0.25, st + 0.12),
                (mid - 0.25, st + 0.12), (mid - 0.35, st), (r2 + 0.02, st)]
        top = _mv(_mrev(seal, segs), 0, 0, B / 2 - rec - st - 0.12)
        bot = _xf(top, rot((1, 0, 0), math.pi))
        # filler (cage + balls) hidden behind the seals, keeps the part a closed volume
        cage = _mtube(r1 - 0.01, r2 + 0.01, B - 2 * (rec + st + 0.12) - 0.1, segs)
        pb.add("seals", _U(top, bot), _SEAL_COLOR.get(key, "#1d1e21"), "Rubber")
        pb.add("cage", _mv(cage, 0, 0, -(B - 2 * (rec + st + 0.12) - 0.1) / 2), "#6d7178", "Steel")
    return Component(key, b["name"], "bearing", pb.parts(), frames, dict(b), {"key": key, **b})


def _lazy_susan(pb, d, D, B):
    """Two stamped zinc-plated rings with a visible ball race and mounting holes."""
    ri, ro = d / 2, D / 2
    rb = (ri + ro) / 2 + 2.5          # ball circle
    tk = 1.0
    ball_r = B / 4 - 0.02
    zc_base = B / 2 - tk / 2          # sheet centre-line height of the flat part
    arch = ball_r + tk / 2            # arch centre-line height over the race
    hw = ball_r * 1.6

    def ring(r0, r1, sgn):
        cl = [(r0, sgn * zc_base), (rb - hw - 1.2, sgn * zc_base), (rb - hw, sgn * (zc_base - 0.4)),
              (rb - hw * 0.45, sgn * arch), (rb + hw * 0.45, sgn * arch), (rb + hw, sgn * (zc_base - 0.4)),
              (rb + hw + 1.2, sgn * zc_base), (r1, sgn * zc_base)]
        poly = LineString(cl).buffer(tk / 2, cap_style=2, join_style=2, mitre_limit=3)
        return _mrev(list(np.asarray(orient(poly, 1.0).exterior.coords)[:-1]), 96)

    low = ring(rb - hw - 3.0, ro, -1)
    up = ring(ri, rb + hw + 3.0, +1)
    lh = (rb + hw + ro) / 2
    uh = (ri + rb - hw) / 2
    low = _D(low, [_mcyl(2.2, B, -B, 16, xy=(lh * math.cos(a), lh * math.sin(a))) for a in np.radians([45, 135, 225, 315])])
    up = _D(up, [_mcyl(2.2, B, 0, 16, xy=(uh * math.cos(a), uh * math.sin(a))) for a in np.radians([0, 90, 180, 270])])
    pb.add("ring_lower", low, C_STEEL, "Steel")
    pb.add("ring_upper", up, "#cfd3d8", "Steel")
    n = int(2 * math.pi * rb / (2 * ball_r + 1.2))
    balls = [_msphere(ball_r, (rb * math.cos(2 * math.pi * k / n), rb * math.sin(2 * math.pi * k / n), 0), 12)
             for k in range(n)]
    pb.add("balls", _U(balls), "#e3e6ea", "Stainless")


def bearing(key: str) -> Component:
    return _copy_component(_bearing_cached(key))


# ------------------------------------------------------------------ fasteners
_PITCH = {"M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8}
_BUTTON = {"M2": (3.5, 1.3, 1.3), "M2.5": (4.7, 1.5, 1.5), "M3": (5.7, 1.65, 2.0), "M4": (7.6, 2.2, 2.5), "M5": (9.5, 2.75, 3.0)}
_FLAT = {"M2": (4.0, 1.2, 1.3), "M2.5": (5.0, 1.5, 1.5), "M3": (6.0, 1.7, 2.0), "M4": (8.0, 2.3, 2.5), "M5": (10.0, 2.8, 3.0)}
_NUT = {"M2": (4.0, 1.6), "M2.5": (5.0, 2.0), "M3": (5.5, 2.4), "M4": (7.0, 3.2), "M5": (8.0, 4.7)}
_WASHER = {"M2": (2.2, 5.0, 0.3), "M2.5": (2.7, 6.0, 0.5), "M3": (3.2, 7.0, 0.5), "M4": (4.3, 9.0, 0.8), "M5": (5.3, 10.0, 1.0)}
_INSERT = {"M2": (3.6, 3.0), "M2.5": (3.6, 4.0), "M3": (4.6, 5.7), "M4": (5.6, 8.1), "M5": (6.4, 9.5)}
FASTENER_KINDS = ("shcs", "bhcs", "fhcs", "nut", "washer", "insert")
_KIND_ALIAS = {"socket": "shcs", "socket_head": "shcs", "cap": "shcs", "button": "bhcs", "button_head": "bhcs",
               "flat": "fhcs", "flat_head": "fhcs", "countersunk": "fhcs", "hex_nut": "nut", "heat_set": "insert",
               "heatset": "insert", "heat_set_insert": "insert"}
FASTENER_LOOK = {"shcs": ("#2b2c30", "Steel"), "bhcs": ("#2b2c30", "Steel"), "fhcs": ("#2b2c30", "Steel"),
                 "nut": (C_STEEL, "Steel"), "washer": (C_STEEL, "Steel"), "insert": (C_BRASS, "Copper")}


def _parse_spec(spec):
    s = str(spec).upper().replace(" ", "")
    size, _, rest = s.partition("X")
    size = size if size in catalog.FASTENER_SIZES else "M3"
    try:
        length = float("".join(ch for ch in rest if ch.isdigit() or ch == ".")) if rest else 0.0
    except ValueError:
        length = 0.0
    return size, length


def _shank(d, L, pitch, z_top=0.0, plain=0.6, threads=True):
    """Threaded shank from z_top down to z_top - L (thread crests as revolved ridges), chamfered tip."""
    r = d / 2
    if L <= 0:
        return MF()
    segs = _segs(r, 16, 24)
    if not threads or L < 2 * pitch:
        return _mv(_mchamcyl(r, L, 0.0, segs, bottom_chamfer=min(0.4 * pitch + 0.1, L * 0.3)), 0, 0, z_top - L)
    rm = r - 0.55 * pitch
    pts = [(0, z_top), (r, z_top), (r, z_top - plain)]
    z = z_top - plain
    tip = z_top - L + pitch * 0.6
    while z - pitch > tip:
        pts += [(rm, z - pitch / 2), (r, z - pitch)]
        z -= pitch
    pts += [(rm, tip), (rm - 0.15 * pitch, z_top - L), (0, z_top - L)]
    return _mrev(pts, segs)


@functools.lru_cache(maxsize=256)
def _fastener_cached(spec, kind):
    kind = _KIND_ALIAS.get(kind, kind)
    size, L = _parse_spec(spec)
    d = float(size[1:])
    hd, hh, key = catalog.FASTENER_SIZES[size]
    p = _PITCH.get(size, 0.5)
    if kind == "shcs":
        L = L or 8.0
        n = max(18, int(hd * 5))
        knurl = []
        for i in range(2 * n):
            a = math.pi * i / n
            rr = hd / 2 if i % 2 == 0 else hd / 2 - 0.09
            knurl.append((rr * math.cos(a), rr * math.sin(a)))
        c = 0.08 * hd
        prof = [(0, 0), (hd / 2 + 0.2, 0), (hd / 2 + 0.2, hh - c), (hd / 2 - c, hh + 0.01), (0, hh + 0.01)]
        head = _I(_mext(Polygon(knurl), hh), _mrev(prof, 2 * n))
        head = _U(head, _mrev([(0, -0.05), (d / 2 + 0.15, -0.05), (d / 2 + 0.15, 0.2), (0, 0.2)], 24))
        head = _D(head, _mxsock(key, hh * 0.6, hh + 0.05))
        return _U(head, _shank(d, L, p))
    if kind == "bhcs":
        L = L or 8.0
        dk, k, s = _BUTTON[size]
        prof = [(0, 0), (dk / 2, 0), (dk / 2, k * 0.18)]
        for t in np.linspace(0.15, 1.0, 7):
            prof.append((dk / 2 * math.cos(t * math.pi / 2) ** 0.8, k * 0.18 + (k * 0.82) * math.sin(t * math.pi / 2)))
        prof.append((0, k))
        head = _D(_mrev(prof, 32), _mxsock(s, k * 0.55, k + 0.05))
        return _U(head, _shank(d, L, p))
    if kind == "fhcs":
        L = L or 10.0
        dk, k, s = _FLAT[size]
        head = _mrev([(0, -k), (d / 2, -k), (dk / 2, -0.12), (dk / 2, 0), (0, 0)], 32)
        head = _D(head, _mxsock(s, k * 0.6, 0.02))
        return _U(head, _shank(d, L - k, p, z_top=-k + 0.01))
    if kind == "nut":
        af, m = _NUT[size]
        body = _mhex(af, m, -m, chamfer=1.0)
        hole = _mrev([(0, -m - 1), (d / 2 * 0.86, -m - 1), (d / 2 * 0.86, -m + 0.1), (d / 2 + 0.15, -m - 0.2),
                      (d / 2 + 0.15, -m - 1)], 24)
        cs = _mrev([(0, -m - 0.5), (d / 2 * 0.86, -m - 0.5), (d / 2 * 0.86, 0.5), (0, 0.5)], 24)
        csk = _mrev([(0, -0.35), (d / 2 * 0.86, -0.35), (d / 2 + 0.3, 0.05), (0, 0.05)], 24)
        cskb = _mrev([(0, -m - 0.05), (d / 2 + 0.3, -m - 0.05), (d / 2 * 0.86, -m + 0.35), (0, -m + 0.35)], 24)
        return _D(body, cs, csk, cskb, hole)
    if kind == "washer":
        d1, d2, h = _WASHER[size]
        return _mv(_mtube(d2 / 2, d1 / 2, h, _segs(d2 / 2, 24, 48), chamfer=min(0.1, h * 0.2)), 0, 0, -h)
    if kind == "insert":
        od, il = _INSERT[size]
        L = L if L and L < 3 * il else il
        n = 18
        knurl = []
        for i in range(2 * n):
            a = math.pi * i / n
            rr = od / 2 if i % 2 == 0 else od / 2 - 0.22
            knurl.append((rr * math.cos(a), rr * math.sin(a)))
        band = L * 0.36
        k1 = _mext(Polygon(knurl), band, -band - 0.25)
        k2 = _xf(_mext(Polygon(knurl), band, -L + 0.2), rot((0, 0, 1), math.pi / n))
        core = _mrev([(0, 0), (od / 2 - 0.15, 0), (od / 2 - 0.15, -0.25), (od / 2 - 0.35, -0.25), (od / 2 - 0.35, -L + 0.2),
                      (od / 2 - 0.5, -L), (0, -L)], 36)
        body = _U(core, k1, k2)
        return _D(body, _mcyl(d / 2 * 0.86, L + 1, -L - 0.5, 20), _mcyl(d / 2 + 0.15, 0.6, -0.45, 20, r2=d / 2 + 0.4))
    raise ValueError(f"unknown fastener kind {kind!r}; use one of {FASTENER_KINDS}")


def fastener(spec: str = "M3x8", kind: str = "shcs") -> trimesh.Trimesh:
    """Fastener mesh. kind: shcs (socket head cap), bhcs (button head), fhcs (flat head countersunk),
    nut, washer, insert (heat-set). spec "M3x8" = size x length (length ignored for nuts / washers)."""
    return _tri(_fastener_cached(str(spec), str(kind))).copy()


def fastener_component(spec: str = "M3x8", kind: str = "shcs") -> Component:
    """fastener() wrapped as a Component with its colour / material and frames."""
    k = _KIND_ALIAS.get(kind, kind)
    size, L = _parse_spec(spec)
    color, mat = FASTENER_LOOK.get(k, ("#2b2c30", "Steel"))
    mesh = fastener(spec, kind)
    zmin = float(mesh.bounds[0][2])
    return Component(f"{size}x{L:g}_{k}" if L else f"{size}_{k}", f"{spec} {k}", "fastener", [("body", mesh, color, mat)],
                     {"seat": np.eye(4), "tip": trans((0, 0, zmin)) @ rot((1, 0, 0), math.pi)},
                     {"size": size, "length": L, "kind": k}, {"key": f"{size}_{k}", "spec": spec, "kind": k})


# ------------------------------------------------------------------ electronics building blocks
# Board parts are built in a local frame with their bottom at z = 0 and placed on the board top.
# Edge connectors (USB, sockets, jacks, terminals): opening faces -x (front at x = 0), body towards +x,
# width along y.
C_PLASTIC = "#18191b"


def _cf(x, y, z, direction):
    """Connector frame at (x, y, z) with +z pointing along `direction` (out along the cable)."""
    return trans((x, y, z)) @ _zto(direction)


def _board(pb, l, w, color, t=1.6, r=0.8, holes=(), xy=(0.0, 0.0), ring=0.5, cut=()):
    """PCB outline (z in [0, t]) with plated holes [(x, y, d)]; gold annular rings and barrels."""
    b = _mrbox(l, w, t, r, 0.0, xy)
    cuts = [_mcyl(dd / 2 + 0.06, t + 1, -0.5, xy=(x, y)) for x, y, dd in holes] + list(cut)
    pb.add("pcb", _D(b, cuts), color, "FR4")
    for x, y, dd in holes:
        if ring > 0:
            ann = _D(_mcyl(dd / 2 + ring, t + 0.06, -0.03, xy=(x, y)), _mcyl(dd / 2, t + 1, -0.5, xy=(x, y)))
            pb.add("pads", ann, C_GOLD, "Copper")
    return t


def _pad(pb, x, y, l, w, z=1.6, deg=0.0):
    pb.add("pads", _xf(_mbox(l, w, 0.035, (0, 0, 0.0175)), _place_xy(x, y, z, deg)), C_GOLD, "Copper")


def _silk(pb, text, x, y, h, deg=0.0, z=1.6, weight=0.14, max_width=None):
    _print(pb, "silk", text, h, _place_xy(x, y, z, deg), C_SILK, relief=0.025, weight=weight, max_width=max_width,
           material="FR4")


def _silk_box(pb, x, y, l, w, lw=0.15, z=1.6, deg=0.0):
    ring = Polygon([(-l / 2, -w / 2), (l / 2, -w / 2), (l / 2, w / 2), (-l / 2, w / 2)])
    ring = ring.difference(ring.buffer(-lw, join_style=2))
    pb.add("silk", _xf(_mext(ring, 0.025), _place_xy(x, y, z, deg)), C_SILK, "FR4")


def _header(pb, x, y, n, rows=1, pitch=2.54, along="x", z0=1.6, spacer=2.5, pin_up=6.0, color=C_HDR, pin_suffix="pins"):
    """Male pin header (pins up). Returns the list of pin (x, y) positions."""
    nx, ny = (n, rows) if along == "x" else (rows, n)
    Lx, Ly = nx * pitch, ny * pitch
    body = _mrbox(Lx - 0.08, Ly - 0.08, spacer, 0.15, z0, (x, y), top=0.2, steps=1)
    notches = [_mbox(0.3, Ly + 1, 0.5, (x - Lx / 2 + i * pitch, y, z0 + spacer)) for i in range(1, nx)]
    notches += [_mbox(Lx + 1, 0.3, 0.5, (x, y - Ly / 2 + j * pitch, z0 + spacer)) for j in range(1, ny)]
    pb.add("headers", _D(body, notches), color, "PA12")
    pins, pos = [], []
    ztop = z0 + spacer + pin_up
    for i in range(nx):
        for j in range(ny):
            px, py = x - Lx / 2 + (i + 0.5) * pitch, y - Ly / 2 + (j + 0.5) * pitch
            pos.append((px, py))
            q = []
            for zz, hw in ((z0 - 1.2, 0.32), (ztop - 0.4, 0.32), (ztop, 0.14)):
                q += [(px - hw, py - hw, zz), (px + hw, py - hw, zz), (px + hw, py + hw, zz), (px - hw, py + hw, zz)]
            pins.append(MF.hull_points(np.array(q)))
    pb.add(pin_suffix, _U(pins), C_GOLD, "Copper")
    return pos


def _gull_leg(lead, h, lift, lw):
    prof = LineString([(-0.25, lift + h * 0.42), (lead * 0.32, lift + h * 0.42), (lead * 0.62, 0.075),
                       (lead, 0.075)]).buffer(0.075, cap_style=2, join_style=2, mitre_limit=3)
    return _ext_y(prof, -lw / 2, lw / 2)


def _ic(pb, x, y, l, w, h=1.0, kind="soic", n=(4, 4), pitch=1.27, lead=1.0, text=None, text_h=None, deg=0.0,
        body=C_IC, z=1.6, text2=None):
    """IC package: soic / tssop (legs on +-y), qfp (all sides), qfn (edge pads), sot23, sot223, plain."""
    T = _place_xy(x, y, z, deg)
    lift = 0.1 if kind in ("soic", "qfp", "sot23", "sot223") else 0.02
    b = _mrbox(l, w, h, min(0.2, 0.08 * min(l, w)), lift, top=min(0.12, h * 0.2), steps=1)
    pr = min(0.32, 0.07 * min(l, w))
    if min(l, w) > 2.2:
        b = _D(b, _mcyl(pr, 0.2, lift + h - 0.06, 12, xy=(-l / 2 + 2.2 * pr + 0.15, -w / 2 + 2.2 * pr + 0.15)))
    pb.add("chips", _xf(b, T), body, "ABS")
    legs = []
    if kind in ("soic", "qfp"):
        leg = _gull_leg(lead, h, lift, pitch * 0.45)
        sides = [((0, w / 2), 90, n[0], l)] + [((0, -w / 2), -90, n[0], l)]
        if kind == "qfp":
            sides += [((l / 2, 0), 0, n[1], w), ((-l / 2, 0), 180, n[1], w)]
        for (ox, oy), ang, cnt, _ in sides:
            for i in range(cnt):
                u = (i - (cnt - 1) / 2) * pitch
                px, py = (ox + u, oy) if ang in (90, -90) else (ox, oy + u)
                legs.append(_xf(leg, _place_xy(px, py, 0, ang)))
    elif kind == "qfn":
        for (ox, oy), ang, cnt in (((0, w / 2), 90, n[0]), ((0, -w / 2), -90, n[0]), ((l / 2, 0), 0, n[1]),
                                   ((-l / 2, 0), 180, n[1])):
            for i in range(cnt):
                u = (i - (cnt - 1) / 2) * pitch
                px, py = (ox + u, oy) if ang in (90, -90) else (ox, oy + u)
                legs.append(_xf(_mbox(0.35, pitch * 0.5, 0.1, (0.0, 0, 0.05)), _place_xy(px, py, 0, ang)))
    elif kind == "sot23":
        leg = _gull_leg(0.55, h, lift, 0.4)
        for px, ang in ((-0.95, -90), (0.95, -90), (0.0, 90)):
            legs.append(_xf(leg, _place_xy(px, (w / 2) * (1 if ang == 90 else -1), 0, ang)))
    elif kind == "sot223":
        leg = _gull_leg(1.4, h, lift, 0.7)
        for px in (-2.3, 0.0, 2.3):
            legs.append(_xf(leg, _place_xy(px, -w / 2, 0, -90)))
        legs.append(_xf(_U(_gull_leg(1.4, h, lift, 3.0)), _place_xy(0, w / 2, 0, 90)))
    if legs:
        pb.add("leads", _xf(_U(legs), T), C_TIN, "Stainless")
    if text:
        th = text_h or min(w * 0.22, 1.2)
        dy = th * 0.75 if text2 else 0.0
        _print(pb, "marking", text, th, T @ trans((0, dy, lift + h)), C_MARK, relief=0.02, max_width=l * 0.86,
               material="ABS")
        if text2:
            _print(pb, "marking", text2, th * 0.8, T @ trans((0, -th * 0.8, lift + h)), C_MARK, relief=0.02,
                   max_width=l * 0.86, material="ABS")


def _smd(pb, x, y, l=1.6, w=0.8, h=0.5, kind="cap", deg=0.0, z=1.6):
    T = _place_xy(x, y, z, deg)
    e = l * 0.2
    pb.add("passives" if kind == "cap" else "chips", _xf(_mbox(l - 2 * e + 0.04, w * 0.98, h * 0.96, (0, 0, h * 0.48)), T),
           C_CERAMIC if kind == "cap" else C_IC, "ABS")
    pb.add("leads", _xf(_U(_mbox(e, w, h, (-l / 2 + e / 2, 0, h / 2)), _mbox(e, w, h, (l / 2 - e / 2, 0, h / 2))), T),
           C_TIN, "Stainless")


_LED_COLORS = {"red": "#ff3b2f", "green": "#36e05a", "blue": "#3a8bff", "orange": "#ffa21f", "white": "#f4f6ff",
               "yellow": "#ffe14a"}


def _led(pb, x, y, color="red", l=1.6, w=0.8, deg=0.0, z=1.6):
    T = _place_xy(x, y, z, deg)
    pb.add("led_body", _xf(_mbox(l, w, 0.3, (0, 0, 0.15)), T), "#e9e7df", "PC")
    pb.add(f"led_{color}", _xf(_mrbox(l * 0.7, w * 0.8, 0.3, 0.1, 0.3, top=0.08, steps=1), T), _LED_COLORS[color], "PC")


def _tact(pb, x, y, l=4.0, w=3.0, h=1.4, cap="#1c1c1e", deg=0.0, z=1.6, round_cap=False):
    """SMD tactile switch: metal body + plunger."""
    T = _place_xy(x, y, z, deg)
    body = _mrbox(l, w, h * 0.7, 0.2, 0, top=0.1, steps=1)
    pb.add("metal", _xf(body, T), C_TIN, "Stainless")
    plunger = (_mcyl(min(l, w) * 0.32, h * 0.4, h * 0.65, 24) if round_cap else
               _mrbox(l * 0.42, w * 0.5, h * 0.4, 0.15, h * 0.65, top=0.08, steps=1))
    pb.add("button", _xf(plunger, T), cap, "PA12")


def _usb_items(kind):
    if kind == "micro":
        D_, outer = 5.4, Polygon([(-3.75, 0.85), (-3.0, 0), (3.0, 0), (3.75, 0.85), (3.75, 2.6), (-3.75, 2.6)])
        tongue = _mbox(D_ - 1.4, 4.2, 0.55, ((D_ - 1.4) / 2 + 0.9, 0, 1.75))
    elif kind == "mini":
        D_, outer = 9.2, Polygon([(-3.85, 1.0), (-3.1, 0), (3.1, 0), (3.85, 1.0), (3.85, 3.9), (-3.85, 3.9)])
        tongue = _mbox(D_ - 2.0, 4.6, 1.0, ((D_ - 2.0) / 2 + 1.2, 0, 2.3))
    else:   # USB-C
        D_ = 7.35
        outer = LineString([(-8.94 / 2 + 1.63, 1.63), (8.94 / 2 - 1.63, 1.63)]).buffer(1.63, quad_segs=6)
        tongue = _mbox(D_ - 1.6, 6.6, 0.7, ((D_ - 1.6) / 2 + 1.0, 0, 1.63))
    inner = outer.buffer(-0.28, join_style=2)
    shell = _D(_ext_x(outer, 0, D_), _ext_x(inner, -0.2, D_ - 0.7))
    return [("metal", shell, C_TIN, "Stainless"), ("plastic", tongue, C_PLASTIC, "PA12")], D_


def _usb(pb, kind, x, y, deg=0.0, z=1.6):
    items, depth = _usb_items(kind)
    pb.put(items, _place_xy(x, y, z, deg))
    return depth


def _socket(pb, x, y, n=3, pitch=2.5, h=4.6, depth=5.0, deg=0.0, z=1.6, color=C_CREAM):
    hs, pins = _bus_socket(pitch, n, None, h, depth)
    T = _place_xy(x, y, z, deg)
    pb.add("connectors", _xf(hs, T), color, "PA12")
    pb.add("pins", _xf(pins, T), C_GOLD, "Copper")


def _ecap(pb, x, y, dia, h, sleeve="#1b1c20", stripe="#a8adb4", z=1.6, deg=0.0):
    r = dia / 2
    T = _place_xy(x, y, z, deg)
    body = _mrev([(0, 0.25), (r - 0.2, 0.25), (r, 0.45), (r, h - 0.55), (r - 0.3, h - 0.2), (r - 0.55, h - 0.2),
                  (r - 0.55, h - 0.45), (0, h - 0.45)], 48)
    groove = _D(_mcyl(r + 1, 0.45, h - 1.6, 48), _mcyl(r - 0.22, 1, h - 1.9, 48))
    pb.add("ecap", _xf(_D(body, groove), T), sleeve, "PC")
    top = _mcyl(r - 0.5, 0.2, h - 0.5, 48)
    vent = [_mbox(r * 1.2, 0.18, 0.2, (0, 0, h - 0.33)), _mbox(0.18, r * 1.2, 0.2, (0, 0, h - 0.33))]
    pb.add("ecap_top", _xf(_D(top, vent), T), "#c9cdd2", "Al6061")
    band = _I(_D(_mcyl(r + 0.03, h - 2.6, 0.7, 48), _mcyl(r - 0.1, h, 0.5, 48)), _mbox(r, r * 0.9, h, (r / 2 + 0.3, 0, h / 2)))
    pb.add("ecap_stripe", _xf(band, T), stripe, "PC")
    pb.add("plastic", _xf(_mcyl(r - 0.1, 0.26, 0.0, 32), T), C_PLASTIC, "PA12")


def _inductor(pb, x, y, s, h, text="100", z=1.6, deg=0.0, color="#202124"):
    T = _place_xy(x, y, z, deg)
    pb.add("inductor", _xf(_mrbox(s, s, h, s * 0.22, 0, top=0.35, steps=2), T), color, "ABS")
    if text:
        _print(pb, "marking", text, s * 0.24, T @ trans((0, 0, h)), C_MARK, relief=0.02, material="ABS")


def _crystal(pb, x, y, l=3.2, w=2.5, h=0.8, z=1.6, deg=0.0):
    pb.add("metal", _xf(_mrbox(l, w, h, 0.25, 0, top=0.12, steps=1), _place_xy(x, y, z, deg)), C_TIN, "Stainless")


def _terminal(pb, x, y, n=2, pitch=5.0, depth=8.0, h=9.0, color="#2a78c8", deg=0.0, z=1.6):
    """Screw terminal block; wire openings face local -x."""
    T = _place_xy(x, y, z, deg)
    W_ = n * pitch
    body = _mrbox(depth, W_ - 0.1, h, 0.3, 0, (depth / 2, 0), top=0.35, steps=1)
    step = _mbox(depth * 0.42 + 0.2, W_ + 1, h * 0.32 + 0.2, (depth * 0.42 / 2 - 0.1, 0, h - h * 0.32 / 2 + 0.1))
    cuts, screws = [step], []
    for i in range(n):
        yy = (i - (n - 1) / 2) * pitch
        sr = pitch * 0.3
        cuts.append(_mcyl(sr, 4.0, h - 2.6, 24, xy=(depth * 0.7, yy)))
        cuts.append(_mbox(4.0, pitch * 0.6, pitch * 0.6, (1.6, yy, h * 0.38)))
        sc = _D(_mcyl(sr - 0.15, 0.8, h - 1.5, 24, xy=(depth * 0.7, yy)), _mbox(sr * 2.4, 0.35, 0.6, (depth * 0.7, yy, h - 0.75)))
        screws.append(sc)
        screws.append(_mbox(0.3, pitch * 0.5, pitch * 0.45, (1.9, yy, h * 0.38)))
    pb.add("terminal", _xf(_D(body, cuts), T), color, "PA12")
    pb.add("metal", _xf(_U(screws), T), C_TIN, "Stainless")


def _dc_jack(pb, x, y, w=9.0, d=14.0, h=10.4, deg=0.0, z=1.6):
    T = _place_xy(x, y, z, deg)
    zc = h * 0.55
    body = _mrbox(d, w, h, 0.4, 0, (d / 2, 0), top=0.4, steps=1)
    boss = _xf(_mcyl(w * 0.46, 1.2, -1.2, 40), trans((0, 0, zc)) @ _zto((-1, 0, 0)))
    body = _D(_U(body, boss), _hole((-1.2, 0, zc), (-1, 0, 0), 3.2, 10.5))
    pin = _xf(_mcyl(1.0, 8.5, 0, 16), trans((1.6, 0, zc)) @ _zto((1, 0, 0)))
    pb.add("plastic", _xf(body, T), C_PLASTIC, "PA12")
    pb.add("metal", _xf(pin, T), C_TIN, "Stainless")


def _module_frame_conn(frames, name, T):
    frames[f"connector_{name}"] = T
    frames.setdefault("connector_a", T)


# ------------------------------------------------------------------ modules
def _mod_arduino_nano(spec, d):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    bl = L - 1.8
    bx = 0.9
    holes = [(bx + sx * (bl / 2 - 1.5), sy * (W / 2 - 1.5), 1.7) for sx in (-1, 1) for sy in (-1, 1)]
    t = _board(pb, bl, W, "#1b5fae", r=0.4, holes=holes, xy=(bx, 0))
    pin_up = max(d["h"] - t - 2.5, 2.0)
    for sy in (-1, 1):
        _header(pb, bx, sy * 7.62, 15, pin_up=pin_up)
    _header(pb, bx + 16.0, 0, 3, rows=2, along="y", pin_up=pin_up)
    _usb(pb, "mini", -L / 2, 0)
    _module_frame_conn(fr, "usb", _cf(-L / 2, 0, t + 1.95, (-1, 0, 0)))
    _ic(pb, bx + 3.2, 0, 6.6, 6.6, 1.0, "qfp", (8, 8), 0.8, 1.0, text="ATMEGA328P", text_h=0.62, deg=45, text2="AU 2318")
    _tact(pb, -7.0, 0, 6.0, 3.5, 1.6, cap=C_WHITE, deg=90)
    for i, c in enumerate(("orange", "green", "red", "blue")):
        _led(pb, -12.2, -4.5 + 3.0 * i, c, deg=90)
    _crystal(pb, -2.2, 4.4, 3.2, 1.3, 0.9)
    for (x, y, dg) in ((-2.0, -4.6, 0), (9.7, 4.9, 90), (9.7, -4.9, 90), (-4.6, 4.6, 90), (-4.6, -4.6, 90)):
        _smd(pb, x, y, kind="cap", deg=dg)
    for (x, y, dg) in ((-10.0, 4.7, 0), (-10.0, -4.7, 0), (11.6, 2.4, 90), (11.6, -2.4, 90)):
        _smd(pb, x, y, kind="res", deg=dg)
    _silk(pb, "NANO", bx + 11.5, 0, 1.1, deg=90)
    for i in range(15):
        for sy in (-1, 1):
            pb.add("silk", _mbox(0.9, 0.18, 0.025, (bx + (i - 7) * 2.54, sy * 5.6, t + 0.0125)), C_SILK, "FR4")
    return pb, fr


def _wroom(pb, x0, x1, y0, half_w, text, t=1.6):
    """ESP32 WROOM-style module: PCB, RF shield, meander antenna, castellated pads (antenna towards +x)."""
    mt = 0.8
    pb.add("module_pcb", _mrbox(x1 - x0, 2 * half_w, mt, 0.3, t, ((x0 + x1) / 2, y0)), "#1d2026", "FR4")
    ant = 6.4
    sx0, sx1 = x0 + 0.5, x1 - ant
    shield = _mrbox(sx1 - sx0, 2 * half_w - 0.8, 2.3, 0.4, t + mt, ((sx0 + sx1) / 2, y0), top=0.25, steps=1)
    pb.add("shield", shield, "#cdd1d6", "Stainless")
    F = trans(((sx0 + sx1) / 2, y0, t + mt + 2.3)) @ rot((0, 0, 1), math.pi / 2)
    _print(pb, "shield_print", text, 1.25, F @ trans((0, 2.2, 0)), "#55595f", relief=0.02, max_width=2 * half_w - 3,
           material="ABS")
    _print(pb, "shield_print", "ESPRESSIF", 0.9, F @ trans((0, 0.0, 0)), "#55595f", relief=0.02, material="ABS")
    _print(pb, "shield_print", "FCC ID 2AC7Z-ESPWROOM", 0.55, F @ trans((0, -1.7, 0)), "#6c7076", relief=0.02,
           max_width=2 * half_w - 3, material="ABS")
    pts = []
    xs = (x1 - ant + 0.9, x1 - 0.8)
    ys = np.linspace(y0 - half_w + 1.4, y0 + half_w - 3.2, 9)
    for i, yy in enumerate(ys):
        a, b = (xs if i % 2 == 0 else xs[::-1])
        pts += [(a, yy), (b, yy)]
    trace = LineString(pts).buffer(0.22, cap_style=2, join_style=2)
    pb.add("pads", _mext(trace, 0.03, t + mt), C_GOLD, "Copper")
    for i in range(13):
        xx = x0 + 1.2 + i * 1.27
        if xx > sx1:
            break
        for sy in (-1, 1):
            pb.add("pads", _mbox(0.9, 0.8, 0.05, (xx, y0 + sy * (half_w - 0.35), t + mt + 0.02)), C_GOLD, "Copper")


def _mod_esp32_devkit(spec, d, s3=False):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    mod_l = 25.5
    bx0, bx1 = -L / 2 + 0.6, L / 2 - 5.5
    bc = (bx0 + bx1) / 2
    holes = []
    t = _board(pb, bx1 - bx0, W, "#16181c", r=1.0, holes=holes, xy=(bc, 0))
    n = 22 if s3 else 19
    row = 11.43 if s3 else 12.7
    pin_up = max(d["h"] - t - 2.5, 2.0)
    for sy in (-1, 1):
        pos = _header(pb, bc, sy * row, n, pin_up=pin_up)
        for px, py in pos:
            pb.add("silk", _mbox(0.95, 0.18, 0.025, (px, py - sy * 2.05, t + 0.0125)), C_SILK, "FR4")
    _wroom(pb, L / 2 - mod_l, L / 2, 0.0, 9.0, "ESP32-S3-WROOM-1" if s3 else "ESP32-WROOM-32E", t)
    if s3:
        for i, yy in enumerate((-4.7, 4.7)):
            _usb(pb, "c", -L / 2, yy)
            _module_frame_conn(fr, "usb" if i == 0 else "uart", _cf(-L / 2, yy, t + 1.63, (-1, 0, 0)))
        _tact(pb, -L / 2 + 10.5, -7.6, 3.6, 2.8, 1.2, deg=90)
        _tact(pb, -L / 2 + 10.5, 7.6, 3.6, 2.8, 1.2, deg=90)
        pb.add("led_body", _mrbox(3.4, 3.4, 0.9, 0.3, t, (-L / 2 + 16.5, -5.5)), "#efefe8", "PC")
        pb.add("led_white", _mcyl(1.2, 0.25, t + 0.9, 24, xy=(-L / 2 + 16.5, -5.5)), "#f6f2ff", "PC")
        _ic(pb, -L / 2 + 17.5, 4.5, 5.0, 5.0, 0.85, "qfn", (7, 7), 0.5, text="CP2102N", text_h=0.55)
        _ic(pb, -L / 2 + 25.5, -5.0, 6.5, 3.5, 1.6, "sot223", text="1117", text_h=0.9)
        _silk(pb, "ESP32-S3-DEVKITC-1", L / 2 - 25.5 - 3.6, 0.0, 0.95, deg=90, max_width=16.0)
    else:
        _usb(pb, "micro", -L / 2, 0)
        _module_frame_conn(fr, "usb", _cf(-L / 2, 0, t + 1.3, (-1, 0, 0)))
        _tact(pb, -L / 2 + 3.4, -8.0, 4.0, 3.0, 1.4, deg=90)
        _tact(pb, -L / 2 + 3.4, 8.0, 4.0, 3.0, 1.4, deg=90)
        _silk(pb, "EN", -L / 2 + 6.6, -8.0, 0.9, deg=90)
        _silk(pb, "BOOT", -L / 2 + 6.6, 8.0, 0.9, deg=90)
        _ic(pb, -L / 2 + 13.0, 4.6, 5.0, 5.0, 0.85, "qfn", (7, 7), 0.5, text="CP2102N", text_h=0.55)
        _ic(pb, -L / 2 + 14.0, -5.2, 6.5, 3.5, 1.6, "sot223", text="AMS1117", text_h=0.7, text2="3.3")
        _led(pb, -L / 2 + 8.5, -3.6, "red", deg=90)
        _silk(pb, "ESP32-DEVKITC", -L / 2 + 21.5, 0.0, 1.05, deg=90, max_width=15.5)
    for (x, y, dg) in ((-L / 2 + 19.5, -2.0, 90), (-L / 2 + 19.5, 2.0, 90), (-L / 2 + 9.5, 3.2, 0),
                       (-L / 2 + 9.5, -6.2, 0), (-L / 2 + 23.0, 7.0, 0)):
        _smd(pb, x, y, kind="cap", deg=dg)
    for (x, y, dg) in ((-L / 2 + 17.0, 0.0, 0), (-L / 2 + 22.0, -8.2, 0), (-L / 2 + 23.0, 8.3, 0)):
        _smd(pb, x, y, kind="res", deg=dg)
    _ic(pb, -L / 2 + 8.8, 5.6, 2.9, 1.3, 1.0, "sot23")
    fr["antenna"] = trans((L / 2 - 3.0, 0, t + 0.8))
    return pb, fr


def _mod_pico(spec, d):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    bl = L - 1.0
    bx = 0.5
    holes = [(bx + sx * (bl / 2 - 2.0), sy * 5.7, 2.1) for sx in (-1, 1) for sy in (-1, 1)]
    cut = []
    xs = [bx + (i - 9.5) * 2.54 for i in range(20)]
    for xx in xs:
        for sy in (-1, 1):
            cut.append(_mcyl(0.55, 3, -1, 16, xy=(xx, sy * W / 2)))
    t = _board(pb, bl, W, "#16793a", t=max(d["h"] - 2.6, 1.0), r=0.8, holes=holes, xy=(bx, 0), cut=cut)
    for xx in xs:
        for sy in (-1, 1):
            pad = Polygon([(-0.85, 0), (0.85, 0), (0.85, 1.9), (-0.85, 1.9)]).difference(Point(0, 1.9).buffer(0.55, 12))
            pad = pad.union(Point(0, 0.3).buffer(0.85, quad_segs=6)).difference(Point(0, 0.3).buffer(0.5, quad_segs=6))
            g = _mext(pad, 0.04)
            T = trans((xx, sy * (W / 2 - 1.9), t)) if sy > 0 else trans((xx, -(W / 2 - 1.9), t)) @ rot((0, 0, 1), math.pi)
            pb.add("pads", _xf(g, T), C_GOLD, "Copper")
    for yy in (-2.54, 0.0, 2.54):
        pb.add("pads", _mcyl(0.75, 0.04, t, 16, xy=(L / 2 - 1.8, yy)), C_GOLD, "Copper")
    _usb(pb, "micro", -L / 2, 0, z=t)
    _module_frame_conn(fr, "usb", _cf(-L / 2, 0, t + 1.3, (-1, 0, 0)))
    _ic(pb, 2.5, 0, 7.0, 7.0, 0.9, "qfn", (14, 14), 0.4, text="RP2040", text_h=1.0, text2="RP2-B2  20/21", z=t)
    _ic(pb, -7.0, 4.2, 4.9, 3.9, 1.4, "soic", (4, 4), 1.27, 1.0, text="W25Q16", text_h=0.6, deg=90, z=t)
    _tact(pb, -13.5, -3.6, 4.2, 3.2, 1.6, cap=C_WHITE, round_cap=True, z=t)
    _silk(pb, "BOOTSEL", -13.5, -6.3, 0.75, z=t)
    _crystal(pb, 9.4, 4.6, 3.2, 2.5, 0.8, z=t)
    _inductor(pb, -15.2, 4.2, 2.6, 1.2, text=None, color="#5d6066", z=t)
    _ic(pb, -17.8, 4.4, 2.0, 2.0, 0.6, "qfn", (3, 3), 0.5, z=t)
    _led(pb, -19.6, -5.4, "green", z=t)
    for (x, y, dg) in ((8.5, -4.0, 0), (8.5, -1.8, 0), (-3.0, -4.6, 90), (-3.0, 4.8, 90), (12.0, 1.0, 90), (-11.0, 1.2, 0)):
        _smd(pb, x, y, 1.0, 0.5, 0.35, kind="cap", deg=dg, z=t)
    _silk(pb, "RASPBERRY PI PICO", 3.0, -6.5, 0.85, z=t)
    _silk(pb, "(C)2020", 15.0, 3.2, 0.7, z=t)
    return pb, fr


def _mod_pca9685(spec, d):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    holes = [(sx * (L / 2 - 2.6), sy * (W / 2 - 2.6), 2.5) for sx in (-1, 1) for sy in (-1, 1)]
    t = _board(pb, L, W, "#1f4fa6", r=1.2, holes=holes)
    pin_up = max(d["h"] - t - 2.5, 2.0)
    rows_y = -W / 2 + 0.6 + 3 * 2.54 / 2
    for b in range(4):
        xb = (b * 5 + 1.5 - 9.0) * 2.54
        _header(pb, xb, rows_y, 4, rows=3, pin_up=pin_up)
        for c in range(4):
            ch = b * 4 + c
            _silk(pb, str(ch), xb + (c - 1.5) * 2.54, rows_y + 4.9, 0.9)
    for sx in (-1, 1):
        _header(pb, sx * (L / 2 - 2.2), 0.0, 6, along="y", pin_up=pin_up)
    _module_frame_conn(fr, "i2c", _cf(-(L / 2 - 2.2), 0.0, t + 2.5 + pin_up, (0, 0, 1)))
    _module_frame_conn(fr, "servo_0", _cf((0 * 5 + 1.5 - 9.0) * 2.54 - 1.5 * 2.54, rows_y, t + 2.5 + pin_up, (0, 0, 1)))
    _terminal(pb, 0.0, W / 2 - 0.4, 2, 3.5, 6.6, 8.0, color="#2f8f3f", deg=-90)
    _module_frame_conn(fr, "power", _cf(0.0, W / 2 - 0.4, t + 3.0, (0, 1, 0)))
    _ic(pb, 14.0, 5.6, 9.7, 4.4, 1.0, "soic", (14, 14), 0.65, 0.9, text="PCA9685PW", text_h=0.75)
    _ecap(pb, -14.5, 6.6, 6.3, d["h"] - t - 0.2)
    _ic(pb, -6.5, 9.0, 2.9, 1.3, 1.0, "sot23")
    _led(pb, -3.0, 9.2, "green")
    for i in range(4):
        _ic(pb, 3.5 + i * 2.6, 1.8, 2.0, 1.0, 0.45, "plain", deg=90)
    for i in range(6):
        _pad(pb, 22.0 + (i % 3) * 1.6 - 1.6, 0.6 + (i // 3) * 1.5, 0.7, 1.0)
    for (x, y, dg) in ((-9.0, 1.6, 0), (-9.0, 3.4, 0), (8.0, 9.6, 0), (20.5, 4.0, 90)):
        _smd(pb, x, y, kind="cap", deg=dg)
    _silk(pb, "16-CHANNEL 12-BIT PWM DRIVER", -8.0, 0.9, 0.85)
    _silk(pb, "PCA9685", 14.0, 9.4, 0.95)
    return pb, fr


def _mod_bus_servo_adapter(spec, d, color="#16589e", title="BUS SERVO ADAPTER"):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    holes = [(sx * (L / 2 - 2.8), sy * (W / 2 - 2.8), 3.0) for sx in (-1, 1) for sy in (-1, 1)]
    t = _board(pb, L, W, color, r=1.5, holes=holes)
    jh = d["h"] - t
    _dc_jack(pb, -L / 2 - 0.4, 7.0, 9.0, 13.5, jh)
    _module_frame_conn(fr, "power", _cf(-L / 2 - 1.6, 7.0, t + jh * 0.55, (-1, 0, 0)))
    _usb(pb, "c", -L / 2 - 0.4, -7.0)
    _module_frame_conn(fr, "usb", _cf(-L / 2 - 0.4, -7.0, t + 1.63, (-1, 0, 0)))
    for i, yy in enumerate((-7.2, 7.2)):
        _socket(pb, L / 2 + 0.3, yy, 3, 2.5, 5.2, 6.0, deg=180)
        _module_frame_conn(fr, f"servo_{'ab'[i]}", _cf(L / 2 + 0.3, yy, t + 2.6, (1, 0, 0)))
    fr["connector_a"] = fr["connector_servo_a"]
    _terminal(pb, L / 2 - 1.0, 0.0, 2, 3.5, 6.4, 7.4, color="#2f8f3f", deg=180)
    pos = _header(pb, 1.5, W / 2 - 3.6, 3, pin_up=5.0)
    pb.add("jumper", _mrbox(5.0, 2.4, 6.0, 0.3, t + 2.3, (pos[0][0] + 1.27, pos[0][1]), top=0.3, steps=1), "#f0c02d", "PA12")
    _silk(pb, "A    B", 1.5, W / 2 - 6.0, 0.9)
    _ic(pb, -5.0, -5.5, 4.0, 4.0, 0.85, "qfn", (6, 6), 0.5, text="CH343P", text_h=0.5)
    _ic(pb, 6.5, -9.0, 6.5, 3.5, 1.6, "sot223", text="AMS1117", text_h=0.6)
    _ic(pb, 6.0, -3.0, 4.9, 3.9, 1.4, "soic", (4, 4), 1.27, 1.0, text="74HC126", text_h=0.55)
    _led(pb, -6.0, 13.6, "red")
    _led(pb, -3.0, 13.6, "green")
    for (x, y, dg) in ((-1.0, -9.5, 0), (-1.0, -11.5, 0), (11.0, -5.5, 90), (-10.0, -1.5, 90), (11.5, 3.5, 90)):
        _smd(pb, x, y, kind="cap", deg=dg)
    _silk(pb, title, 1.5, 3.6, 1.05, max_width=19.0)
    _silk(pb, "WAVESHARE", 1.5, 1.4, 0.8)
    return pb, fr


def _mod_u2d2(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    shell = _mrbox(L, W, H, 2.4, 0, top=1.8, bot=1.0, steps=3)
    cut = [_seam(L, W, 2.4, H * 0.45, (0, 0))]
    usb_pocket = _xf(_mext(LineString([(-3.0, 0), (3.0, 0)]).buffer(1.75, quad_segs=4), 4.0),
                     trans((-L / 2 - 0.5, 0, H * 0.5)) @ _AX_X)
    cut.append(usb_pocket)
    cut.append(_mbox(5.0, 7.6, 5.0, (L / 2 - 2.3, -3.6, H * 0.66)))
    cut.append(_mbox(5.0, 7.6, 5.0, (L / 2 - 2.3, 3.6, H * 0.30)))
    cut.append(_mbox(28.0, 11.5, 0.5, (2.0, 0, H - 0.2)))
    for i in range(3):
        cut.append(_mcyl(0.7, 1.0, H - 0.8, 16, xy=(-L / 2 + 6.0, -3.0 + 3.0 * i)))
    pb.add("case", _D(shell, cut), "#141518", "ABS")
    pb.add("label", _mbox(27.6, 11.1, 0.2, (2.0, 0, H - 0.35)), "#2b2e33", "PC")
    _print(pb, "print", "U2D2", 3.6, trans((-1.0, 0.8, H - 0.25)), "#f0f0ec", weight=0.17)
    _print(pb, "print", "ROBOTIS", 1.3, trans((9.5, -3.4, H - 0.25)), "#b9bcc1")
    for i, c in enumerate(("red", "green", "blue")):
        pb.add(f"led_{c}", _mcyl(0.62, 0.45, H - 0.7, 16, xy=(-L / 2 + 6.0, -3.0 + 3.0 * i)), _LED_COLORS[c], "PC")
    items, _ = _usb_items("micro")
    pb.put(items, trans((-L / 2 + 0.6, 0, H * 0.5 - 1.3)))
    _module_frame_conn(fr, "usb", _cf(-L / 2, 0, H * 0.5, (-1, 0, 0)))
    hs, pins = _bus_socket(2.5, 3, 7.4, 4.6, 4.6)
    T = trans((L / 2 - 4.6 + 0.05, -3.6, H * 0.66 - 2.3)) @ rot((0, 0, 1), math.pi) @ trans((-4.6, 0, 0))
    pb.add("connectors", _xf(hs, T), C_CREAM, "PA12")
    pb.add("pins", _xf(pins, T), C_GOLD, "Copper")
    hs2, pins2 = _bus_socket(2.0, 3, 7.4, 4.6, 4.6)
    T2 = trans((L / 2 - 4.6 + 0.05, 3.6, H * 0.30 - 2.3)) @ rot((0, 0, 1), math.pi) @ trans((-4.6, 0, 0))
    pb.add("connectors", _xf(hs2, T2), C_CREAM, "PA12")
    pb.add("pins", _xf(pins2, T2), C_GOLD, "Copper")
    _module_frame_conn(fr, "ttl", _cf(L / 2, -3.6, H * 0.66, (1, 0, 0)))
    fr["connector_a"] = fr["connector_ttl"]
    _module_frame_conn(fr, "rs485", _cf(L / 2, 3.6, H * 0.30, (1, 0, 0)))
    return pb, fr


def _mod_buck_small(spec, d):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    pins = [(-L / 2 + 1.3, (i - 2) * 2.54, 1.0) for i in range(5)]
    holes = pins + [(L / 2 - 1.9, sy * (W / 2 - 1.9), 2.0) for sy in (-1, 1)]
    t = _board(pb, L, W, "#22603a", r=0.6, holes=holes, ring=0.42)
    _inductor(pb, 2.0, 0.0, 6.0, d["h"] - t - 0.1, text="100")
    _ic(pb, -3.5, -4.3, 3.0, 3.0, 0.85, "qfn", (4, 4), 0.5, text="36V", text_h=0.5)
    _smd(pb, -3.4, 4.7, 3.2, 1.6, 1.2, kind="cap", deg=0)
    _smd(pb, 7.8, 4.4, 3.2, 1.6, 1.2, kind="cap", deg=0)
    _smd(pb, 7.8, -4.4, 3.2, 1.6, 1.2, kind="cap", deg=0)
    _smd(pb, -1.6, -6.9, 1.6, 0.8, 0.5, kind="res")
    _ic(pb, -2.0, 6.6, 2.6, 1.6, 1.0, "plain")
    for i, lab in enumerate(("VIN", "GND", "VOUT", "EN", "PG")):
        _silk(pb, lab, -L / 2 + 3.6, (i - 2) * 2.54, 0.6)
    _silk(pb, "5V 3A", 7.2, 0.0, 0.9, deg=90)
    _module_frame_conn(fr, "in", _cf(-L / 2 + 1.3, -5.08, t, (0, 0, 1)))
    _module_frame_conn(fr, "out", _cf(-L / 2 + 1.3, 0.0, t, (0, 0, 1)))
    return pb, fr


def _heatsink(pb, x, y, lx, ly, h, fins=5, z=1.6):
    base = 2.0
    fw = 1.1
    prof = [Polygon([(-lx / 2, 0), (lx / 2, 0), (lx / 2, base), (-lx / 2, base)])]
    for i in range(fins):
        fx = -lx / 2 + fw / 2 + i * (lx - fw) / (fins - 1)
        prof.append(Polygon([(fx - fw / 2, 0), (fx + fw / 2, 0), (fx + fw * 0.35, h), (fx - fw * 0.35, h)]))
    g = unary_union(prof)
    pb.add("heatsink", _mv(_ext_y(g, -ly / 2, ly / 2), x, y, z), C_ALU, "Al6061")


def _to220(pb, x, y, deg=0.0, z=1.6, text="XL4016E1", legs=5):
    T = _place_xy(x, y, z, deg)
    body = _mbox(4.5, 10.0, 9.0, (2.25, 0, 3.0 + 4.5))
    tab = _D(_mbox(1.3, 10.0, 15.0, (0.65, 0, 3.0 + 7.5)), _xf(_mcyl(1.8, 4, -2, 20), trans((0.65, 0, 3.0 + 12.0)) @ _zto((1, 0, 0))))
    pb.add("chips", _xf(body, T), C_IC, "ABS")
    pb.add("metal", _xf(tab, T), C_TIN, "Stainless")
    pb.add("leads", _xf(_U([_mbox(0.5, 0.8, 3.2, (2.6, (i - (legs - 1) / 2) * 1.7, 1.6)) for i in range(legs)]), T),
           C_TIN, "Stainless")
    _print(pb, "marking", text, 0.8, T @ trans((4.51, 0, 8.0)) @ rot((0, 1, 0), math.pi / 2) @ rot((0, 0, 1), math.pi / 2),
           C_MARK, relief=0.02, max_width=8.5, material="ABS")


def _toroid(pb, x, y, r_out, r_in, h, turns=22, z=1.6, wire=0.9):
    c = 0.9
    sec = Polygon([(r_in + c, 0), (r_out - c, 0), (r_out, c), (r_out, h - c), (r_out - c, h), (r_in + c, h), (r_in, h - c),
                   (r_in, c)])
    core = _mrev(list(np.asarray(sec.exterior.coords)[:-1]), 64)
    pb.add("toroid", _mv(core, x, y, z + 0.7), "#d9c867", "PA12")
    loop = sec.buffer(wire + 0.03, quad_segs=4).difference(sec.buffer(0.03, join_style=2))
    turn = _ext_y(loop, -wire / 2, wire / 2)     # one turn in the (r, z) plane, wire thickness along the tangent
    out = [_xf(turn, rot((0, 0, 1), 2 * math.pi * k / turns + 0.2)) for k in range(turns)]
    leads = [_capsules([(r_out * math.cos(a), r_out * math.sin(a), 0.2), (r_out * math.cos(a) * 1.04, r_out * math.sin(a) * 1.04,
                                                                           -0.6)], wire / 2, 8) for a in (0.2 - 0.12, 0.2 + 2 * math.pi * (turns - 1) / turns + 0.12)]
    pb.add("winding", _mv(_U(out + leads), x, y, z + 0.7), C_COPPER, "Copper")


def _trimpot(pb, x, y, deg=0.0, z=1.6):
    T = _place_xy(x, y, z, deg)
    pb.add("trimpot", _xf(_mrbox(9.5, 4.8, 9.8, 0.3, 0, top=0.4, steps=1), T), "#2158c9", "PA12")
    scr = _D(_mcyl(1.2, 1.2, 9.8, 20, xy=(-3.4, 0)), _mbox(3.0, 0.35, 0.6, (-3.4, 0, 10.8)))
    pb.add("brass", _xf(scr, T), C_BRASS, "Copper")


def _mod_buck_servo(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    holes = [(sx * (L / 2 - 3.2), sy * (W / 2 - 3.2), 3.2) for sx in (-1, 1) for sy in (-1, 1)]
    t = _board(pb, L, W, "#1d4f9c", r=1.0, holes=holes)
    hs_h = H - t - 0.3
    _terminal(pb, -L / 2 - 0.5, 0.0, 2, 5.0, 8.0, 10.0, color="#2f6fc6")
    _terminal(pb, L / 2 + 0.5, 0.0, 2, 5.0, 8.0, 10.0, color="#2f6fc6", deg=180)
    _module_frame_conn(fr, "in", _cf(-L / 2 - 0.5, 0.0, t + 3.5, (-1, 0, 0)))
    _module_frame_conn(fr, "out", _cf(L / 2 + 0.5, 0.0, t + 3.5, (1, 0, 0)))
    for sy in (-1, 1):
        _heatsink(pb, -12.5, sy * 12.0, 13.0, 20.0, hs_h)
        _to220(pb, -5.6, sy * 12.0, text="XL4016E1" if sy < 0 else "SS54", legs=5 if sy < 0 else 2)
    _toroid(pb, 10.5, 12.5, 10.5, 5.0, 8.5)
    _ecap(pb, 6.5, -12.5, 10.0, 16.0, sleeve="#1c1d22", stripe="#c2c7cd")
    _ecap(pb, 17.5, -12.5, 10.0, 16.0, sleeve="#1c1d22", stripe="#c2c7cd")
    _trimpot(pb, -1.0, -21.0)
    _trimpot(pb, 10.0, -21.0)
    _led(pb, 20.5, 2.8, "red")
    for (x, y, dg) in ((-1.0, 3.0, 90), (2.0, -3.5, 0), (14.0, -3.0, 0), (-1.0, -14.0, 90)):
        _smd(pb, x, y, 2.0, 1.25, 0.6, kind="res" if x < 0 else "cap", deg=dg)
    _ic(pb, 3.0, 0.4, 4.9, 3.9, 1.4, "soic", (4, 4), 1.27, 1.0, text="LM358", text_h=0.6, deg=90)
    _silk(pb, "DC-DC 20A", 9.0, -5.6, 1.4)
    _silk(pb, "IN", -L / 2 + 6.5, 7.5, 1.2)
    _silk(pb, "OUT", L / 2 - 7.5, 7.5, 1.2)
    return pb, fr


def _mod_psu(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    s = H / 35.0
    shell = _mrbox(L, W, H, 9.0 * s, 0, top=6.0 * s, bot=3.0 * s, steps=4)
    c14 = Polygon([(-12.0, -8.0), (12.0, -8.0), (12.0, 4.0), (8.0, 8.0), (-8.0, 8.0), (-12.0, 4.0)])
    c14 = affinity.scale(c14, s, s, origin=(0, 0))
    zc = H * 0.5
    frame_cut = _xf(_mext(c14.buffer(1.2 * s, join_style=2), 1.2), trans((-L / 2 - 0.4, 0, zc)) @ _AX_X)
    sock_cut = _xf(_mext(c14.buffer(-1.5 * s, join_style=2), 9.0 * s), trans((-L / 2 - 0.5, 0, zc)) @ _AX_X)
    lab = (6.0 * s, 0.0)
    cut = [_seam(L, W, 9.0 * s, H * 0.5, (0, 0), depth=0.5, width=0.7), frame_cut, sock_cut,
           _mbox(84 * s, 40 * s, 0.6, (lab[0], lab[1], H - 0.25))]
    pb.add("case", _D(shell, cut), "#1a1b1e", "PC")
    pb.add("label", _mbox(83.4 * s, 39.4 * s, 0.25, (lab[0], lab[1], H - 0.42)), "#b7bbc1", "Al6061")
    v = spec.get("out_v", 12.0)
    a = spec.get("out_a", 10.0)
    T = trans((lab[0], lab[1], H - 0.3))
    _print(pb, "print", "AC/DC ADAPTER", 3.2 * s, T @ trans((0, 12.5 * s, 0)), "#1d1f22", weight=0.16)
    _print(pb, "print", f"{v:g}V  {a:g}A", 8.0 * s, T @ trans((0, 2.0 * s, 0)), "#1d1f22", weight=0.17)
    _print(pb, "print", "INPUT: 100-240V~ 50/60HZ 2.5A", 1.9 * s, T @ trans((0, -8.0 * s, 0)), "#33363b", max_width=76 * s)
    _print(pb, "print", f"OUTPUT: {v:g}V = {a:g}A  {v * a:g}W MAX", 1.9 * s, T @ trans((0, -11.8 * s, 0)), "#33363b",
           max_width=76 * s)
    _print(pb, "print", "CE  FCC  ROHS", 1.6 * s, T @ trans((0, -15.4 * s, 0)), "#55595f")
    # IEC C14 inlet: dark socket face + three flat pins
    pins = [_mbox(6.0 * s, 1.0 * s, 3.4 * s, (-L / 2 + 6.0 * s, yy * s, zc + zz * s)) for yy, zz in ((-4.0, -1.5), (4.0, -1.5))]
    pins.append(_mbox(6.0 * s, 3.4 * s, 1.0 * s, (-L / 2 + 6.0 * s, 0, zc + 3.6 * s)))
    pb.add("metal", _U(pins), C_TIN, "Stainless")
    pb.add("plastic", _xf(_mext(c14.buffer(-1.5 * s, join_style=2), 0.6), trans((-L / 2 + 8.4 * s, 0, zc)) @ _AX_X),
           "#0e0f11", "PA12")
    _module_frame_conn(fr, "ac", _cf(-L / 2, 0, zc, (-1, 0, 0)))
    # DC output: strain relief, cable stub, barrel plug
    relief = _xf(_mrev([(0, 0), (6.0 * s, 0), (5.6 * s, 3.0), (3.2 * s, 16.0 * s), (0, 16.0 * s)], 40),
                 trans((L / 2 - 1.0, 0, zc)) @ _zto((1, 0, 0)))
    pb.add("cable_relief", relief, "#121315", "PA12")
    x0 = L / 2 + 15.0 * s
    cr, pr = 2.3 * s, 4.5 * s
    pend = np.array([x0 + 44 * s, 11.0 * s, pr])
    pdir = np.array([1.0, 0.38, 0.0]) / np.linalg.norm([1.0, 0.38, 0.0])
    cab = _capsules([(x0, 0, zc), (x0 + 9 * s, 0, zc - 2.5 * s), (x0 + 19 * s, 1.5 * s, cr + 1.0 * s),
                     (x0 + 30 * s, 5.5 * s, cr), (pend[0] + 0.5 * s, pend[1], pend[2])], cr, 14)
    pb.add("cable", cab, "#141416", "Rubber")
    plug = _mrev([(0, 0), (3.0 * s, 0), (pr, 4.0 * s), (pr, 18.0 * s), (pr - 0.3 * s, 19.0 * s), (0, 19.0 * s)], 32)
    Tp = trans(pend) @ _zto(pdir)
    pb.add("plug", _xf(plug, Tp), "#141416", "PA12")
    pb.add("plug_tip", _xf(_D(_mcyl(2.75 * s, 9.5 * s, 18.8 * s, 32), _mcyl(1.1 * s, 12 * s, 18 * s, 16)), Tp), C_CHROME, "Stainless")
    fr["connector_dc"] = trans(pend + pdir * 28.3 * s) @ _zto(pdir)
    fr["connector_a"] = fr["connector_dc"]
    pb.add("led_green", _mcyl(1.3 * s, 0.5, H - 0.45, 20, xy=(L / 2 - 14.0 * s, 0)), _LED_COLORS["green"], "PC")
    for sx in (-1, 1):
        for sy in (-1, 1):
            pb.add("feet", _mcyl(4.0 * s, 1.0, -0.6, 24, xy=(sx * (L / 2 - 14 * s), sy * (W / 2 - 10 * s))), C_RUBBER, "Rubber")
    return pb, fr


def _mod_fsr402(spec, d):
    pb, fr = _PB(), {}
    D_ = d["l"]
    tail = float(d.get("tail", 37.0))
    th = d["h"]
    tw = 7.6
    tab_l = 6.0
    disc = Point(0, 0).buffer(D_ / 2, quad_segs=24)
    neck = Polygon([(D_ * 0.25, -tw / 2), (D_ / 2 + tail - tab_l + 0.5, -tw / 2), (D_ / 2 + tail - tab_l + 0.5, tw / 2),
                    (D_ * 0.25, tw / 2)])
    sub = disc.union(neck.buffer(0.6, join_style=1).buffer(-0.6, join_style=1))
    pb.add("substrate", _mext(sub, th - 0.03), "#c9a35a", "PC")
    pb.add("active", _D(_mcyl(7.4, 0.03, th - 0.03, 64), _mcyl(6.35, 1, th - 0.5, 64)), "#2a2622", "PC")
    pb.add("active", _mcyl(5.9, 0.03, th - 0.03, 64), "#3b352e", "PC")
    tr = unary_union([LineString([(6.2, yy), (D_ / 2 + tail - tab_l, yy)]).buffer(0.55, cap_style=2) for yy in (-1.6, 1.6)])
    pb.add("traces", _mext(tr, 0.025, th - 0.03), "#9fa5ab", "PC")
    tabs = []
    for yy in (-1.6, 1.6):
        tabs.append(_mrbox(tab_l, 2.2, 0.9, 0.25, -0.25, (D_ / 2 + tail - tab_l / 2, yy), top=0.2, steps=1))
    pb.add("tabs", _U(tabs), C_TIN, "Stainless")
    _module_frame_conn(fr, "a", _cf(D_ / 2 + tail, 0.0, th / 2, (1, 0, 0)))
    fr["sense"] = trans((0, 0, th))
    return pb, fr


def _mod_camera(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    holes = [(sx * (L / 2 - 2.0), sy * (W / 2 - 2.0), 2.2) for sx in (-1, 1) for sy in (-1, 1)]
    t = _board(pb, L, W, "#16181c", r=1.2, holes=holes)
    hb = 4.6
    base = _mrbox(14.6, 14.6, hb, 0.6, t, top=0.4, steps=1)
    ears = _mrbox(22.0, 5.0, 1.6, 2.4, t, top=0.3, steps=1)
    ears = _xf(ears, rot((0, 0, 1), math.pi / 4))
    ears = _D(ears, [_mcyl(1.0, 3, t - 1, 16, xy=(sx * 7.8 * 0.7071, sx * 7.8 * 0.7071)) for sx in (-1, 1)])
    rest = H - t - hb
    barrel = _mrev([(0, 0), (6.9, 0), (6.9, rest * 0.42), (7.45, rest * 0.46), (7.45, rest * 0.78), (6.6, rest * 0.84),
                    (6.0, rest * 0.84), (6.0, rest - 0.4), (5.6, rest), (0, rest)], 64)
    ridges = [_xf(_mbox(0.6, 0.5, rest * 0.30, (7.45, 0, rest * 0.62)), rot((0, 0, 1), 2 * math.pi * k / 30)) for k in range(30)]
    barrel = _D(_U(barrel, ridges), _mcyl(4.2, 1.5, rest - 0.6, 48))
    pb.add("lens_holder", _U(base, ears, _mv(barrel, 0, 0, t + hb)), "#151618", "PA12")
    pb.add("lens_ring", _D(_mcyl(4.2, 0.25, t + hb + rest - 0.62, 48), _mcyl(3.6, 1, t + hb + rest - 1, 48)), C_CHROME, "Stainless")
    pb.add("glass", _mrev([(0, 0), (3.6, 0), (3.6, 0.3), (2.6, 0.55), (0, 0.62)], 48).translate((0, 0, t + hb + rest - 0.62)),
           "#1b2a44", "Acrylic")
    _socket(pb, L / 2 + 0.2, 0.0, 4, 2.0, 4.5, 5.6, deg=180)
    _module_frame_conn(fr, "usb", _cf(L / 2 + 0.2, 0.0, t + 2.2, (1, 0, 0)))
    _ic(pb, -10.5, 7.5, 4.0, 4.0, 0.85, "qfn", (6, 6), 0.5, text="SN9C", text_h=0.55)
    _crystal(pb, -10.5, -8.0, 3.2, 2.5, 0.8)
    for (x, y, dg) in ((10.0, -10.5, 0), (10.0, 10.5, 0), (-12.0, 0.5, 90)):
        _smd(pb, x, y, kind="cap", deg=dg)
    fr["optical"] = trans((0, 0, H))
    return pb, fr


def _mod_imu(spec, d):
    pb, fr = _PB(), {}
    L, W = d["l"], d["w"]
    holes = [(sx * (L / 2 - 2.2), sy * (W / 2 - 2.2), 2.5) for sx in (-1, 1) for sy in (-1, 1)]
    hdr = [((i - 3) * 2.54, -W / 2 + 1.6, 1.0) for i in range(7)]
    t = _board(pb, L, W, "#1a3a7a", r=2.2, holes=holes + hdr, ring=0.45)
    hh = max(d["h"] - t, 1.2)
    for sx in (-1, 1):
        hs, pins = _bus_socket(1.0, 4, 6.0, hh, 4.2)
        T = trans((sx * L / 2 + (0.2 if sx > 0 else -0.2), 2.5, t)) @ rot((0, 0, 1), math.pi if sx > 0 else 0.0)
        pb.add("connectors", _xf(hs, T), C_CREAM, "PA12")
        pb.add("pins", _xf(pins, T), C_GOLD, "Copper")
        _module_frame_conn(fr, "qt_" + ("b" if sx > 0 else "a"), _cf(sx * L / 2, 2.5, t + hh / 2, (sx, 0, 0)))
    _ic(pb, 0.0, 2.6, 5.2, 3.8, 1.1, "plain", text="BNO085", text_h=0.6)
    _ic(pb, -4.9, 6.5, 2.9, 1.3, 1.0, "sot23", deg=0)
    _crystal(pb, 4.6, 6.5, 2.0, 1.2, 0.6)
    _led(pb, 6.0, -2.5, "green")
    for (x, y, dg) in ((-4.0, -1.6, 90), (4.0, -1.6, 90), (-1.5, 6.6, 0), (1.5, -2.6, 0)):
        _smd(pb, x, y, 1.0, 0.5, 0.35, kind="cap", deg=dg)
    _silk(pb, "BNO085 9-DOF", 0.0, -3.4, 0.8)
    for i, lab in enumerate(("VIN", "3V", "GND", "SCL", "SDA", "INT", "RST")):
        _silk(pb, lab, (i - 3) * 2.54, -W / 2 + 3.5, 0.5)
    return pb, fr


def _mod_estop(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    hb = H * 0.58
    box = _mrbox(L, W, hb, 3.0, 0, top=1.6, bot=0.8, steps=3)
    screws = [(sx * (L / 2 - 3.6), sy * (W / 2 - 3.6)) for sx in (-1, 1) for sy in (-1, 1)]
    cut = [_seam(L, W, 3.0, hb - 5.0, (0, 0), depth=0.4, width=0.5)] + [_mcyl(1.6, 1.2, hb - 1.0, 16, xy=p) for p in screws]
    pb.add("box", _D(box, cut), "#f2c200", "PA12")
    for p in screws:
        pb.add("metal", _mv(_pan_screw(2.8, 1.0), p[0], p[1], hb - 1.0), C_STEEL, "Stainless")
    F = _face((0, -W / 2 - 0.0, hb * 0.48), (0, -1, 0))
    _print(pb, "print", "EMERGENCY", 3.3, F @ trans((0, 2.3, 0)), "#141414", relief=0.06, weight=0.17, max_width=L - 8)
    _print(pb, "print", "STOP", 3.3, F @ trans((0, -2.6, 0)), "#141414", relief=0.06, weight=0.17)
    collar = _mrev([(0, 0), (15.0, 0), (15.0, 2.2), (14.4, 3.2), (11.0, 3.6), (0, 3.6)], 64)
    pb.add("collar", _mv(collar, 0, 0, hb - 0.1), "#25272b", "PA12")
    stem = _mcyl(9.0, 2.5, hb + 3.3, 48)
    R = min(L, W) / 2 - 1.0
    top = H
    z0 = hb + 5.0
    head = _mrev([(0, z0), (R * 0.86, z0), (R * 0.98, z0 + 1.2), (R, z0 + 2.6), (R * 0.97, z0 + 4.0), (R * 0.88, z0 + 5.2),
                  (R * 0.72, top - 0.9), (R * 0.5, top - 0.2), (0, top)], 96)
    pb.add("head", _U(head, stem), "#d7261e", "PC")
    arrow = []
    for sgn in (-1, 1):
        a0, a1 = (math.radians(20), math.radians(70)) if sgn > 0 else (math.radians(200), math.radians(250))
        arc = LineString([(5.4 * math.cos(a), 5.4 * math.sin(a)) for a in np.linspace(a0, a1, 10)]).buffer(0.45, cap_style=2)
        tip = Polygon([(5.4 * math.cos(a1) + 1.3 * math.cos(a1), 5.4 * math.sin(a1) + 1.3 * math.sin(a1)),
                       (5.4 * math.cos(a1) - 1.3 * math.cos(a1), 5.4 * math.sin(a1) - 1.3 * math.sin(a1)),
                       (5.4 * math.cos(a1 + 0.4), 5.4 * math.sin(a1 + 0.4))])
        arrow.append(arc.union(tip))
    pb.add("print", _mext(unary_union(arrow), 0.08, top - 0.15), "#f5f5f0", "PC")
    gl = _xf(_U(_mhex(10.0, 3.0, 0, chamfer=0.6), _mrev([(0, 3.0), (4.6, 3.0), (4.6, 6.0), (3.8, 8.0), (2.4, 8.4), (0, 8.4)], 32)),
             trans((L / 2 - 0.2, 0, hb * 0.4)) @ _zto((1, 0, 0)))
    pb.add("gland", gl, "#1b1c1f", "PA12")
    _module_frame_conn(fr, "a", _cf(L / 2 + 8.2, 0, hb * 0.4, (1, 0, 0)))
    fr["button"] = trans((0, 0, top))
    fr["panel"] = trans((0, 0, hb))
    return pb, fr


def _mod_usb_c_panel(spec, d):
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    xf = -L / 2 + 4.0
    flange = _mrbox(1.8, W, H, 1.2, 0, (xf + 0.9, 0), top=0.3, bot=0.3, steps=1)
    body = _mrbox(L - 4.0 - 1.8 - 5.0, W - 2.6, H - 1.8, 1.6, 0.9, (xf + 1.8 + (L - 10.8) / 2, 0), top=0.8, bot=0.8, steps=2)
    nose = _mrbox(4.0, 10.6, 4.6, 1.8, H / 2 - 2.3, (xf - 2.0, 0), top=0.4, bot=0.4, steps=1)
    port = LineString([(-8.94 / 2 + 1.63, H / 2), (8.94 / 2 - 1.63, H / 2)]).buffer(1.63, quad_segs=6)
    housing = _D(_U(flange, body, nose), _ext_x(port, -L / 2 - 1, xf + 4))
    pb.add("plastic", housing, "#17181a", "PA12")
    items, _ = _usb_items("c")
    pb.put(items, trans((-L / 2, 0, H / 2 - 1.63)))
    relief = _xf(_mrev([(0, 0), (2.8, 0), (2.2, 3.2), (1.8, 5.0), (0, 5.0)], 32), trans((L / 2 - 5.0, 0, H / 2)) @ _zto((1, 0, 0)))
    pb.add("plastic", relief, "#17181a", "PA12")
    pb.add("cable", _capsules([(L / 2, 0, H / 2), (L / 2 + 10, 0, H / 2 - 0.5), (L / 2 + 16, 2.0, H / 2 - 2.0)], 1.6, 12),
           "#141416", "Rubber")
    _module_frame_conn(fr, "a", _cf(L / 2 + 16, 2.0, H / 2 - 2.0, (1, 0, 0)))
    fr["port"] = _cf(-L / 2, 0, H / 2, (-1, 0, 0))
    fr["panel"] = _cf(xf, 0, H / 2, (-1, 0, 0))
    return pb, fr


def _mod_power_inlet(spec, d):
    """Panel-mount 5.5 x 2.1 DC jack: front collar, threaded barrel, hex nut, rear body with solder lugs."""
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    zc = H / 2
    ax = lambda m, x0: _xf(m, trans((x0, 0, zc)) @ _zto((1, 0, 0)))
    collar = ax(_D(_mrev([(0, 0), (W / 2, 0), (W / 2, 1.2), (W / 2 - 0.3, 1.5), (0, 1.5)], 48), _mcyl(3.2, 4, -1, 32)), -L / 2)
    thread_r = 3.9
    pts = [(0, 0), (thread_r, 0)]
    for k in range(9):
        pts += [(thread_r - 0.3, 0.35 + k * 0.7), (thread_r, 0.7 + k * 0.7)]
    pts += [(thread_r, 7.0), (0, 7.0)]
    thread = ax(_D(_mrev(pts, 40), _mcyl(3.2, 10, -1, 32)), -L / 2 + 1.5)
    nut = _xf(_mhex(min(W, H / 1.1547) * 0.995, 2.0, 0, chamfer=0.5), trans((-L / 2 + 3.0, 0, zc)) @ _zto((1, 0, 0)))
    nut = _D(nut, ax(_mcyl(thread_r + 0.05, 3, -0.5, 40), -L / 2 + 3.0))
    body = _mrbox(6.0, 8.0, 8.0, 0.6, zc - 4.0, (-L / 2 + 8.5 + 3.0, 0), top=0.4, bot=0.4, steps=1)
    pin = ax(_mcyl(1.05, 8.0, 0, 16), -L / 2 + 0.6)
    lugs = [_D(_mbox(L / 2 - (-L / 2 + 14.5) + 0.2, 0.4, 2.2, ((-L / 2 + 14.5 + L / 2) / 2, yy, zc + zz)),
               _xf(_mcyl(0.5, 2, -1, 12), trans((L / 2 - 1.0, yy, zc + zz)) @ _zto((0, 1, 0)))) for yy, zz in ((-2.2, 1.5), (2.2, 1.5), (0, -2.6))]
    pb.add("plastic", _U(collar, body), "#17181a", "PA12")
    pb.add("metal", _U(thread, nut, pin, lugs), C_CHROME, "Stainless")
    _module_frame_conn(fr, "a", _cf(L / 2, 0, zc, (1, 0, 0)))
    fr["port"] = _cf(-L / 2, 0, zc, (-1, 0, 0))
    fr["panel"] = _cf(-L / 2 + 1.5, 0, zc, (-1, 0, 0))
    return pb, fr


def _mod_generic(spec, d):
    """Fallback for catalog entries without a dedicated model: role-styled board / box."""
    role = spec.get("role", "")
    L, W, H = d["l"], d["w"], d["h"]
    if role == "supply":
        return _mod_psu(spec, d)
    pb, fr = _PB(), {}
    if H > 0.45 * min(L, W) and role not in ("controller", "computer", "servo_driver", "power"):
        pb.add("case", _mrbox(L, W, H, min(4.0, W * 0.15), 0, top=min(2.0, H * 0.2), bot=0.8, steps=3), "#1c1d20", "PC")
        _print(pb, "print", spec.get("name", "").split("(")[0][:18].upper(), min(H * 0.18, 4.0), trans((0, 0, H)), "#dcdcd8",
               max_width=L * 0.8)
        _module_frame_conn(fr, "a", _cf(L / 2, 0, H / 2, (1, 0, 0)))
        return pb, fr
    color = {"controller": "#16181c", "computer": "#1c7a3e", "servo_driver": "#16589e", "power": "#1d4f9c",
             "sensor": "#1a3a7a"}.get(role, "#1d5a3a")
    holes = [(sx * (L / 2 - 3.0), sy * (W / 2 - 3.0), 2.7) for sx in (-1, 1) for sy in (-1, 1)] if min(L, W) > 18 else []
    t = _board(pb, L, W, color, r=min(3.0, W * 0.08), holes=holes)
    hmax = max(H - t, 0.8)
    _ic(pb, -L * 0.1, 0, min(L, W) * 0.3, min(L, W) * 0.3, min(1.2, hmax), "qfp" if min(L, W) > 25 else "qfn",
        (10, 10) if min(L, W) > 25 else (6, 6), 0.5, 0.8, text=spec.get("key", "").upper()[:10], text_h=min(L, W) * 0.03 + 0.4)
    if hmax > 6 and min(L, W) > 30:
        _usb(pb, "c", -L / 2, -W * 0.25)
        _module_frame_conn(fr, "usb", _cf(-L / 2, -W * 0.25, t + 1.63, (-1, 0, 0)))
        _header(pb, L * 0.1, W / 2 - 3.5, int(min(20, L * 0.6 / 2.54)), rows=2, pin_up=min(6.0, hmax - 2.5))
    if hmax > 3:
        _ecap(pb, L * 0.3, -W * 0.2, min(max(hmax * 0.6, 4.0), W * 0.35, 10.0), hmax - 0.1)
    _socket(pb, L / 2 + 0.2, 0.0, 3, 2.5, min(4.6, hmax), 5.0, deg=180)
    _module_frame_conn(fr, "a", _cf(L / 2 + 0.2, 0.0, t + min(4.6, hmax) / 2, (1, 0, 0)))
    for i in range(4):
        _smd(pb, -L * 0.3 + i * 2.5, -W * 0.3, kind="cap")
    _silk(pb, spec.get("name", "").split("(")[0][:22].upper(), 0, W * 0.3, min(1.4, W * 0.06), max_width=L * 0.7)
    return pb, fr


def _mod_rpi5(spec, d):
    """Raspberry Pi 5: GPIO header, USB / Ethernet stack, metal-lidded BCM2712, RP1, LPDDR4X, micro-HDMI, USB-C."""
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    bl = L - 2.0
    bx = -1.0
    x0 = bx - bl / 2
    holes = [(x0 + 3.5, sy * (W / 2 - 3.5), 2.7) for sy in (-1, 1)] + [(x0 + 61.5, sy * (W / 2 - 3.5), 2.7) for sy in (-1, 1)]
    t = _board(pb, bl, W, "#177a3f", r=3.0, holes=holes, xy=(bx, 0))
    _header(pb, x0 + 3.5 + 29.0, W / 2 - 3.5, 20, rows=2, pin_up=6.0)
    _module_frame_conn(fr, "gpio", _cf(x0 + 32.5, W / 2 - 3.5, t + 8.5, (0, 0, 1)))
    xe = L / 2
    hu = H - t
    for i, (yy, tongue) in enumerate(((1.5, "#2f6fd6"), (-16.5, C_PLASTIC))):
        shell = _D(_mrbox(17.5, 13.1, hu, 0.4, t, (xe - 17.5 / 2, yy), top=0.3, steps=1),
                   [_mbox(14.0, 12.0, 5.6, (xe - 7.0 + 0.2, yy, t + zz)) for zz in (hu * 0.27, hu * 0.73)])
        pb.add("metal", shell, C_TIN, "Stainless")
        for zz in (hu * 0.27, hu * 0.73):
            pb.add("usb_tongue" if i == 0 else "plastic", _mbox(13.0, 11.0, 1.9, (xe - 6.5, yy, t + zz - 1.0)), tongue, "PA12")
        _module_frame_conn(fr, f"usb{3 if i == 0 else 2}", _cf(xe, yy, t + hu * 0.73, (1, 0, 0)))
    eth = _D(_mrbox(21.2, 16.0, 13.5, 0.4, t, (xe - 21.2 / 2, 19.0), top=0.3, steps=1),
             _mbox(16.0, 12.0, 9.0, (xe - 8.0 + 0.2, 19.0, t + 6.2)))
    pb.add("metal", eth, C_TIN, "Stainless")
    pb.add("plastic", _mbox(15.0, 11.6, 3.0, (xe - 7.5, 19.0, t + 9.6)), C_PLASTIC, "PA12")
    for yy in (19.0 - 5.0, 19.0 + 5.0):
        pb.add("led_green" if yy < 19 else "led_orange", _mbox(0.4, 2.0, 1.4, (xe + 0.05, yy, t + 12.0)),
               _LED_COLORS["green" if yy < 19 else "orange"], "PC")
    _module_frame_conn(fr, "ethernet", _cf(xe, 19.0, t + 6.0, (1, 0, 0)))
    _usb(pb, "c", x0 + 11.2 - 4.47, -W / 2 - 1.0, deg=90, z=t)
    _module_frame_conn(fr, "power", _cf(x0 + 11.2, -W / 2 - 1.0, t + 1.63, (0, -1, 0)))
    for i, xx in enumerate((x0 + 26.0, x0 + 39.5)):
        hd = Polygon([(-3.25, 0.6), (-2.6, 0), (2.6, 0), (3.25, 0.6), (3.25, 3.0), (-3.25, 3.0)])
        shell = _D(_ext_x(hd, 0, 7.5), _ext_x(hd.buffer(-0.3, join_style=2), -0.2, 6.8))
        pb.add("metal", _xf(shell, _place_xy(xx, -W / 2 - 1.0, t, 90)), C_TIN, "Stainless")
        _module_frame_conn(fr, f"hdmi{i}", _cf(xx, -W / 2 - 1.0, t + 1.5, (0, -1, 0)))
    soc = (x0 + 29.0, -2.0)
    _ic(pb, soc[0], soc[1], 16.0, 16.0, 1.2, "plain", body="#2a5a3a")
    pb.add("shield", _mrbox(13.0, 13.0, 0.6, 1.0, t + 1.2, soc, top=0.2, steps=1), "#cfd3d8", "Stainless")
    _print(pb, "shield_print", "BCM2712", 1.6, trans((soc[0], soc[1] + 1.2, t + 1.8)), "#5f6369", relief=0.02, material="ABS")
    _print(pb, "shield_print", "D0 2023", 1.0, trans((soc[0], soc[1] - 1.6, t + 1.8)), "#6c7076", relief=0.02, material="ABS")
    _ic(pb, x0 + 50.0, -1.0, 12.0, 11.0, 1.0, "plain", text="MICRON", text_h=1.2, text2="D9ZCV")
    _ic(pb, x0 + 61.0, -14.0, 9.0, 9.0, 0.9, "qfn", (12, 12), 0.5, text="RP1", text_h=1.6)
    _ic(pb, x0 + 12.0, 8.0, 5.0, 5.0, 0.9, "qfn", (8, 8), 0.5, text="DA9091", text_h=0.6)
    for xx, yy in ((x0 + 46.0, 15.5), (x0 + 60.0, 15.5)):
        pb.add("fpc", _mrbox(17.0, 4.6, 1.2, 0.2, t, (xx, yy)), "#2b2520", "PA12")
        pb.add("fpc_latch", _mbox(17.0, 1.2, 0.6, (xx, yy - 2.4, t + 1.0)), "#7a5a3a", "PA12")
    pb.add("fpc", _mrbox(4.6, 13.0, 1.2, 0.2, t, (x0 + 3.2, -4.0)), "#2b2520", "PA12")
    _socket(pb, x0 + 68.5, W / 2 - 4.5, 4, 1.0, 2.6, 4.2, deg=-90)
    _tact(pb, x0 + 2.5, -W / 2 + 9.0, 3.6, 2.6, 1.3, deg=90)
    _led(pb, x0 + 2.5, -W / 2 + 14.0, "red", deg=90)
    _led(pb, x0 + 2.5, -W / 2 + 16.5, "green", deg=90)
    for i in range(10):
        _smd(pb, x0 + 20.0 + (i % 5) * 2.6, 10.0 + (i // 5) * 2.0, kind="cap" if i % 2 else "res", deg=90)
    _silk(pb, "RASPBERRY PI 5", x0 + 30.0, -W / 2 + 13.5, 1.6)
    _silk(pb, "(C) RASPBERRY PI LTD 2023", x0 + 30.0, -W / 2 + 11.0, 0.9)
    fr["connector_a"] = fr["connector_power"]
    return pb, fr


def _mod_battery(spec, d):
    """Hard-case Li-ion pack: rubberised case, label, end panel with DC jack, rocker switch and fuel-gauge LEDs."""
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    case = _mrbox(L, W, H, 6.0, 0, top=3.5, bot=2.0, steps=4)
    zc = H / 2
    cut = [_seam(L, W, 6.0, H * 0.5, (0, 0), depth=0.4, width=0.6), _mbox(L * 0.62, W * 0.62, 0.6, (-L * 0.06, 0, H - 0.25)),
           _hole((L / 2, -W * 0.22, zc), (1, 0, 0), 3.4, 9.0),
           _mbox(4.0, 9.0, 6.4, (L / 2, W * 0.12, zc))]
    pb.add("case", _D(case, cut), "#1d1e21", "PC")
    pb.add("label", _mbox(L * 0.62 - 0.6, W * 0.62 - 0.6, 0.25, (-L * 0.06, 0, H - 0.43)), "#2d6fd0", "PC")
    v = spec.get("out_v", 12.0)
    T = trans((-L * 0.06, 0, H - 0.3))
    _print(pb, "print", f"{v:g}V  LI-ION", min(W * 0.11, 6.0), T @ trans((0, W * 0.12, 0)), "#f4f6fa", weight=0.17)
    _print(pb, "print", "5000MAH  60WH  DC 5.5X2.1", min(W * 0.045, 2.6), T @ trans((0, -W * 0.06, 0)), "#dfe6f2",
           max_width=L * 0.55)
    _print(pb, "print", "RECHARGEABLE BATTERY PACK", min(W * 0.035, 2.0), T @ trans((0, -W * 0.16, 0)), "#c9d4e6",
           max_width=L * 0.55)
    pb.add("metal", _xf(_mcyl(1.05, 8.0, -8.5, 16), trans((L / 2, -W * 0.22, zc)) @ _zto((1, 0, 0))), C_TIN, "Stainless")
    rocker = _mrbox(2.8, 8.2, 5.6, 0.6, zc - 2.8, (L / 2 - 1.0, W * 0.12), top=0.6, steps=2)
    pb.add("switch", _xf(rocker, trans((L / 2 - 1.0, W * 0.12, zc)) @ rot((0, 1, 0), 0.12) @ trans((-(L / 2 - 1.0), -W * 0.12, -zc))),
           "#141416", "PA12")
    _print(pb, "print", "O  I", 2.2, _face((L / 2 + 0.45, W * 0.12, zc), (1, 0, 0)), "#f0f0f0")
    for i in range(4):
        pb.add("led_green", _xf(_mcyl(0.8, 0.5, -0.3, 16), trans((L / 2, W * 0.36, zc - 4.5 + i * 3.0)) @ _zto((1, 0, 0))),
               _LED_COLORS["green"], "PC")
    _module_frame_conn(fr, "dc", _cf(L / 2, -W * 0.22, zc, (1, 0, 0)))
    for sx in (-1, 1):
        for sy in (-1, 1):
            pb.add("feet", _mcyl(4.0, 1.0, -0.6, 24, xy=(sx * (L / 2 - 12), sy * (W / 2 - 10))), C_RUBBER, "Rubber")
    return pb, fr


def _mod_potted_dcdc(spec, d):
    """Potted DC-DC converter brick: label, red/black input leads, USB-C output lead."""
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    ears = _D(_mrbox(L, W * 0.62, 1.8, 2.0, 0, top=0.4, steps=1),
              [_mcyl(1.7, 3, -1, 16, xy=(sx * (L / 2 - 3.2), 0)) for sx in (-1, 1)])
    body = _mrbox(L - 13.0, W, H, 3.0, 0, top=2.0, bot=0.6, steps=3)
    body = _D(body, _mbox(L - 21.0, W - 7.0, 0.5, (0, 0, H - 0.2)))
    pb.add("case", _U(ears, body), "#16171a", "PC")
    pb.add("label", _mbox(L - 21.6, W - 7.6, 0.2, (0, 0, H - 0.35)), "#e9e9e4", "PC")
    v_in, v_out = 12, spec.get("out_v", 5.0)
    T = trans((0, 0, H - 0.25))
    _print(pb, "print", "DC-DC CONVERTER", min(W * 0.1, 2.2), T @ trans((0, W * 0.16, 0)), "#1d1f22", max_width=L - 25)
    _print(pb, "print", f"{v_in}V > {v_out:g}V {spec.get('out_a', 5):g}A", min(W * 0.16, 3.4), T @ trans((0, -W * 0.06, 0)),
           "#1d1f22", weight=0.17, max_width=L - 25)
    _print(pb, "print", "USB-C PD OUTPUT", min(W * 0.07, 1.5), T @ trans((0, -W * 0.24, 0)), "#3a3d42", max_width=L - 25)
    xi = -(L - 13.0) / 2
    for yy, col, suf in ((-2.0, "#c3261f", "wire_red"), (2.0, "#1a1a1a", "wire_black")):
        pb.add(suf, _capsules([(xi + 1.0, yy, H * 0.45), (xi - 9.0, yy, H * 0.42), (xi - 15.0, yy * 1.6, H * 0.3)], 1.05, 10),
               col, "PC")
    _module_frame_conn(fr, "in", _cf(xi - 15.0, 0, H * 0.3, (-1, 0, 0)))
    xo = (L - 13.0) / 2
    pb.add("cable", _capsules([(xo - 1.0, 0, H * 0.45), (xo + 10.0, 0, H * 0.42), (xo + 16.0, 1.5, H * 0.3)], 1.6, 12),
           "#141416", "Rubber")
    _module_frame_conn(fr, "usb_c", _cf(xo + 16.0, 1.5, H * 0.3, (1, 0, 0)))
    fr["connector_a"] = fr["connector_in"]
    return pb, fr


def _mod_usb_c_plug(spec, d):
    """USB-C cable end: plug shell, overmould with grip ribs, strain relief, cable stub."""
    pb, fr = _PB(), {}
    L, W, H = d["l"], d["w"], d["h"]
    zc = H / 2
    port = LineString([(-8.3 / 2 + 1.25, zc), (8.3 / 2 - 1.25, zc)]).buffer(1.25, quad_segs=6)
    x0 = -L / 2
    shell = _D(_ext_x(port, x0, x0 + 6.7), _ext_x(port.buffer(-0.25, join_style=2), x0 - 0.5, x0 + 5.5))
    pb.add("metal", shell, C_CHROME, "Stainless")
    pb.add("plastic", _ext_x(LineString([(-2.9, zc), (2.9, zc)]).buffer(0.32), x0 + 0.8, x0 + 6.0), C_PLASTIC, "PA12")
    om_l = L * 0.5
    om = _mrbox(om_l, W, H, H / 2 - 0.05, 0, (x0 + 6.7 + om_l / 2, 0), top=1.0, bot=1.0, steps=3)
    om = _D(om, [_mbox(0.5, W + 1, 0.5, (x0 + 6.7 + om_l * 0.55 + i * 1.3, 0, H - 0.1)) for i in range(5)])
    pb.add("overmold", om, "#202124", "PA12")
    rx = x0 + 6.7 + om_l
    relief = _xf(_mrev([(0, 0), (H / 2 - 0.3, 0), (H * 0.36, (L / 2 - rx) * 0.9), (H * 0.3, L / 2 - rx), (0, L / 2 - rx)], 32),
                 trans((rx, 0, zc)) @ _zto((1, 0, 0)))
    pb.add("overmold", relief, "#202124", "PA12")
    pb.add("cable", _capsules([(L / 2 - 0.5, 0, zc), (L / 2 + 14.0, 0.0, zc - 0.5), (L / 2 + 24.0, 4.0, zc - 1.2)], 1.6, 12),
           "#141416", "Rubber")
    _module_frame_conn(fr, "a", _cf(L / 2 + 24.0, 4.0, zc - 1.2, (1, 0, 0)))
    fr["plug"] = _cf(x0, 0, zc, (-1, 0, 0))
    return pb, fr


_MODULE_BUILDERS = {
    "arduino_nano": _mod_arduino_nano,
    "esp32_devkit": _mod_esp32_devkit,
    "esp32_s3_devkit": lambda s, d: _mod_esp32_devkit(s, d, s3=True),
    "rp2040_pico": _mod_pico,
    "pca9685_board": _mod_pca9685,
    "bus_servo_adapter": _mod_bus_servo_adapter,
    "seeed_bus_adapter": lambda s, d: _mod_bus_servo_adapter(s, d, color="#1b6b3f", title="BUS SERVO DRIVER"),
    "u2d2": _mod_u2d2,
    "buck_5v": _mod_buck_small,
    "buck_servo": _mod_buck_servo,
    "psu_12v": _mod_psu,
    "psu_6v": _mod_psu,
    "fsr402": _mod_fsr402,
    "wrist_camera": _mod_camera,
    "imu": _mod_imu,
    "estop": _mod_estop,
    "usb_c_breakout": _mod_usb_c_panel,
    "xt30_inlet": _mod_power_inlet,
    "usb_camera": _mod_camera,
    "raspberry_pi_5": _mod_rpi5,
    "battery_12v": _mod_battery,
    "dc_dc_12v_5v_usbc": _mod_potted_dcdc,
    "usb_c_cable_2pack": _mod_usb_c_plug,
}


@functools.lru_cache(maxsize=128)
def _module_cached(key):
    spec = catalog.get(key)
    d = {k: float(v) if isinstance(v, (int, float)) else v for k, v in spec["dims"].items()}
    builder = _MODULE_BUILDERS.get(key, _mod_generic)
    try:
        pb, frames = builder(spec, d)
    except Exception:   # never let a cosmetic detail take the pipeline down
        if builder is _mod_generic:
            raise
        pb, frames = _mod_generic(spec, d)
    frames = dict(frames)
    frames["mount"] = np.eye(4)
    frames.setdefault("connector_a", _cf(d["l"] / 2, 0, d["h"] / 2, (1, 0, 0)))
    kind = "sensor" if spec.get("role") == "sensor" else "electronics"
    return Component(key, spec["name"], kind, pb.parts(), frames, dict(spec["dims"]), spec)


def module(key: str) -> Component:
    return _copy_component(_module_cached(key))


# ------------------------------------------------------------------ catalogue showcase
def _safe(name: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in name.lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def showcase(row_width: float = 430.0, gap: float = 14.0):
    """Assembly with every catalogued purchased part laid out on a grid (actuators, horns + bearings,
    fasteners, modules), each resting on z = 0. For the parts-library view and visual QA."""
    from .assembly import Assembly, Part

    asm = Assembly("component_showcase")
    rows = [
        [actuator(k) for k in catalog.ACTUATORS],
        [horn(k) for k in catalog.ACTUATORS] + [bearing(k) for k in catalog.BEARINGS],
        [fastener_component(f"{sz}x{8 if sz in ('M2', 'M2.5') else 12}", kind)
         for kind in FASTENER_KINDS for sz in catalog.FASTENER_SIZES],
        [module(k) for k in catalog.MODULES],
    ]
    y = 0.0
    n = 0
    for row in rows:
        x, row_depth = 0.0, 0.0
        small = row and row[0].kind == "fastener"
        g = 4.0 if small else gap
        for comp in row:
            ball = comp.mesh().bounds
            w, dpt = ball[1][0] - ball[0][0], ball[1][1] - ball[0][1]
            if x > 0 and x + w > row_width:
                y -= row_depth + g
                x, row_depth = 0.0, 0.0
            T = trans((x - ball[0][0], y - ball[1][1], -ball[0][2]))
            for suffix, mesh, color, material in comp.parts:
                m = mesh.copy()
                m.apply_transform(T)
                asm.add_part(Part(name=f"p{n:03d}_{_safe(comp.key)}_{_safe(suffix)}", link=asm.root, mesh=m, kind=comp.kind,
                                  material=material, color=color, process="purchased", sku=comp.key,
                                  group=f"{_safe(comp.key)}_{_safe(suffix)}", meta={"component": comp.key}))
            n += 1
            x += w + g
            row_depth = max(row_depth, dpt)
        y -= row_depth + 2.5 * gap
    asm.meta = {"showcase": True, "components": n}
    return asm
