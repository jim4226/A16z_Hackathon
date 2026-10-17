"""Previews for the Import dialog: a posed GLB with a tool-point marker, and a PNG thumbnail drawn on the CPU.

  robot_glb(asm, ready, "preview.glb")        # the robot at its ready pose + an orange TCP cone (z = approach)
  render_png([(mesh, "#d9d9d9")], "thumb.png")  # flat-shaded, z-buffered, no GPU or display needed

The thumbnail is what the dialog shows when the Studio's 3D viewer isn't available (a headless server, a browser
without WebGL), so the person can still check units and orientation before building.
"""

from __future__ import annotations

import math

import numpy as np

BG = (18, 21, 28)
TCP_COLOR = "#ff9152"


def _rgba(color: str, alpha: int = 255) -> list:
    c = color.lstrip("#")
    return [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha]


def tcp_marker(T: np.ndarray, length: float = 45.0):
    """A cone pointing along the tool's z axis (the approach direction) and a small ball at the tool point."""
    import trimesh

    cone = trimesh.creation.cone(radius=length * 0.16, height=length * 0.55, sections=20)
    cone.apply_translation([0, 0, length * 0.45])          # tip ahead of the tool point, along +z
    shaft = trimesh.creation.cylinder(radius=length * 0.05, height=length * 0.5, sections=12)
    shaft.apply_translation([0, 0, length * 0.2])
    ball = trimesh.creation.icosphere(subdivisions=2, radius=length * 0.12)
    m = trimesh.util.concatenate([cone, shaft, ball])
    m.apply_transform(np.asarray(T, float))
    m.visual.face_colors = _rgba(TCP_COLOR)
    return m


def robot_meshes(asm, q: dict, tcp: bool = True) -> list:
    """[(posed mesh, colour)] per part, plus the TCP marker."""
    T = asm.world_transforms(q)
    out = []
    for p in asm.parts:
        m = p.mesh.copy()
        m.apply_transform(T[p.link])
        out.append((m, p.color))
    if tcp and "tcp" in T:
        out.append((tcp_marker(T["tcp"]), TCP_COLOR))
    return out


def glb_bytes(meshes: list) -> bytes:
    import trimesh

    parts = []
    for m, color in meshes:
        m = m.copy()
        m.visual.face_colors = _rgba(color)
        parts.append(m)
    return trimesh.Scene(trimesh.util.concatenate(parts)).export(file_type="glb")


def robot_glb(asm, q: dict, path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(glb_bytes(robot_meshes(asm, q)))
    return path


def _shade(color, normals, light):
    base = np.array(_rgba(color)[:3], float)
    k = 0.32 + 0.68 * np.abs(normals @ light)
    return np.clip(base[None, :] * k[:, None], 0, 255)


def render(meshes: list, size=(480, 360), azimuth: float = -125.0, elevation: float = 24.0, margin: int = 14) -> np.ndarray:
    """RGB uint8 image of [(mesh, colour)] in a z-up frame, orthographic, viewed from (azimuth, elevation) degrees."""
    W, H = size
    img = np.empty((H, W, 3), np.uint8)
    img[:] = BG
    zb = np.full((H, W), -np.inf)
    if not meshes:
        return img
    a, e = math.radians(azimuth), math.radians(elevation)
    d = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])   # towards the camera
    r = np.cross([0.0, 0.0, 1.0], d)
    r /= np.linalg.norm(r)
    u = np.cross(d, r)
    light = d * 0.8 + u * 0.45 - r * 0.25
    light /= np.linalg.norm(light)
    allv = np.vstack([m.vertices for m, _ in meshes])
    c = (allv.min(0) + allv.max(0)) / 2
    xs, ys = (allv - c) @ r, (allv - c) @ u
    span = max(xs.max() - xs.min(), 1e-6), max(ys.max() - ys.min(), 1e-6)
    s = min((W - 2 * margin) / span[0], (H - 2 * margin) / span[1])
    ox = W / 2 - s * (xs.max() + xs.min()) / 2
    oy = H / 2 + s * (ys.max() + ys.min()) / 2
    tx, ty, tz, tc = [], [], [], []
    for m, color in meshes:
        P = m.vertices - c
        px = P @ r * s + ox
        py = oy - P @ u * s
        pz = P @ d
        F = m.faces
        tx.append(px[F])
        ty.append(py[F])
        tz.append(pz[F])
        tc.append(_shade(color, m.face_normals, light).astype(np.uint8))
    tx, ty, tz, tc = np.vstack(tx), np.vstack(ty), np.vstack(tz), np.vstack(tc)
    sp, sz, sc = [], [], []
    # every triangle's centroid is a sample (tiny triangles cover no pixel centre but still show)
    cx, cy = tx.mean(1), ty.mean(1)
    ok = (cx >= 0) & (cx < W) & (cy >= 0) & (cy < H)
    sp.append(cy[ok].astype(int) * W + cx[ok].astype(int))
    sz.append(tz[ok].mean(1))
    sc.append(tc[ok])
    # pixel centres inside each triangle, in batches of triangles with similar bounding boxes
    x0 = np.floor(tx.min(1)).astype(int)
    y0 = np.floor(ty.min(1)).astype(int)
    ext = np.maximum(np.ceil(tx.max(1)) - x0, np.ceil(ty.max(1)) - y0).astype(int) + 1
    den = (ty[:, 1] - ty[:, 2]) * (tx[:, 0] - tx[:, 2]) + (tx[:, 2] - tx[:, 1]) * (ty[:, 0] - ty[:, 2])
    valid = np.abs(den) > 1e-9
    lo = 0
    for k in (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024):
        sel = np.nonzero(valid & (ext > lo) & (ext <= k))[0]
        lo = k
        for chunk in np.array_split(sel, max(1, len(sel) * k * k // 2_000_000 + 1)):
            if not len(chunk):
                continue
            g = np.arange(k)
            gx = x0[chunk, None, None] + g[None, None, :] + 0.5
            gy = y0[chunk, None, None] + g[None, :, None] + 0.5
            X, Y, Z, dn = tx[chunk], ty[chunk], tz[chunk], den[chunk]
            w0 = ((Y[:, 1] - Y[:, 2])[:, None, None] * (gx - X[:, 2, None, None]) +
                  (X[:, 2] - X[:, 1])[:, None, None] * (gy - Y[:, 2, None, None])) / dn[:, None, None]
            w1 = ((Y[:, 2] - Y[:, 0])[:, None, None] * (gx - X[:, 2, None, None]) +
                  (X[:, 0] - X[:, 2])[:, None, None] * (gy - Y[:, 2, None, None])) / dn[:, None, None]
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (gx >= 0) & (gx < W) & (gy >= 0) & (gy < H)
            ti, yi, xi = np.nonzero(inside)
            if not len(ti):
                continue
            zz = w0[ti, yi, xi] * Z[ti, 0] + w1[ti, yi, xi] * Z[ti, 1] + w2[ti, yi, xi] * Z[ti, 2]
            sp.append((gy[ti, yi, 0].astype(int)) * W + gx[ti, 0, xi].astype(int))
            sz.append(zz)
            sc.append(tc[chunk][ti])
    p, z, col = np.concatenate(sp), np.concatenate(sz), np.concatenate(sc)
    order = np.lexsort((z, p))                     # per pixel, nearest sample last
    p, z, col = p[order], z[order], col[order]
    last = np.r_[p[1:] != p[:-1], True]
    p, z, col = p[last], z[last], col[last]
    flat = img.reshape(-1, 3)
    flat[p] = col
    zb.reshape(-1)[p] = z
    return img


def png_bytes(img: np.ndarray) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_png(meshes: list, path: str | None = None, size=(480, 360), **kw) -> bytes:
    data = png_bytes(render(meshes, size, **kw))
    if path:
        with open(path, "wb") as fh:
            fh.write(data)
    return data
