"""Normalize a URDF and its meshes into a self-contained Morph robot bundle.

  rep = normalize("work/repo/follower.urdf", bundle="work/repo", out_dir="work/cand0")
  # -> work/cand0/source.urdf with relative "meshes/<name>.stl" paths, plus a report:
  #    {"urdf", "name", "root", "meshes": {"total", "found", "missing", "searched"}, "renamed", "warnings", ...}

What it fixes (each verified on real robots: SO-100, Koch v1.1, OpenManipulator-X, Menagerie):
* package://pkg/... paths: looked up through every package.xml <name> in the bundle (then a folder named pkg);
  file:// and absolute paths are only accepted when they resolve inside the bundle. Every mesh must stay
  inside the bundle (realpath + commonpath); a missing mesh is reported, never silently dropped.
* Meshes are validated (face cap, malformed files) and copied into meshes/ (binary STL as is, other formats
  converted to STL) so the bundle loads with plain relative paths on every OS.
* Link and joint names become the ASCII ids Morph uses ([a-z0-9_], unique); a link named "world" (which makes
  the generated MuJoCo model fail) and Morph's own reserved names are renamed.
* Gazebo, transmission and ros2_control blocks are dropped; floating/planar joints become fixed (reported).

finalize() then adds a `morph_base` root (yaw + lift so the arm reaches along +x with its base on z = 0) and
a `tcp` link with a full frame (z = approach axis), as derived by autoprofile.
"""

from __future__ import annotations

import io
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET

import numpy as np

from .common import LIMITS, ImportFailed, contained, parse_xml, parse_xml_text

RESERVED = {"world": "world_link", "morph_base": "morph_base_orig", "tcp": "tcp_orig"}
KEEP = ("link", "joint", "material")
MESH_OK = {"stl"}
MESH_CONVERT = {"obj", "ply", "off", "glb", "gltf", "dae", "3mf"}


def safe_name(name: str) -> str:
    """Same rule as urdf_import._safe, so names stay fixed points when load_urdf applies it again."""
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_") or "part"


def packages(bundle: str) -> dict:
    """ROS package name -> folder, for every package.xml inside the bundle."""
    out = {}
    for dp, dns, fns in os.walk(bundle):
        dns[:] = [d for d in dns if not d.startswith(".") and not os.path.islink(os.path.join(dp, d))]
        if "package.xml" in fns:
            try:
                name = (parse_xml(os.path.join(dp, "package.xml"), 1 << 20).findtext("name") or "").strip()
            except ImportFailed:
                name = ""
            out.setdefault(name or os.path.basename(dp), dp)
    return out


def _bundle_index(bundle: str) -> dict:
    idx = {}
    for dp, dns, fns in os.walk(bundle):
        dns[:] = [d for d in dns if not d.startswith(".") and not os.path.islink(os.path.join(dp, d))]
        for f in fns:
            idx.setdefault(f.lower(), []).append(os.path.join(dp, f))
    return idx


def resolve_mesh(ref: str, urdf_dir: str, bundle: str, pkgs: dict, index: dict | None = None) -> tuple[str | None, str]:
    """(real path inside the bundle, how) or (None, why). Never returns a path outside the bundle."""
    ref = (ref or "").strip()
    if not ref:
        return None, "empty filename"
    cands, how = [], ""
    if ref.startswith("package://"):
        pkg, _, rest = ref[len("package://"):].partition("/")
        root = pkgs.get(pkg)
        if root is None:
            dirs = [dp for dp, dns, _ in os.walk(bundle) if os.path.basename(dp) == pkg]
            root = dirs[0] if len(dirs) == 1 else None
        if root:
            cands.append(os.path.join(root, *rest.split("/")))
        how = f"package {pkg}"
        parts = rest.split("/")
        for i in range(len(parts) - 1):          # package://pkg/meshes/x.stl -> meshes/x.stl next to the URDF
            cands.append(os.path.join(urdf_dir, *parts[i:]))
    else:
        p = ref[len("file://"):] if ref.startswith("file://") else ref
        cands.append(p if os.path.isabs(p) else os.path.join(urdf_dir, *p.replace("\\", "/").split("/")))
        how = "path"
    for c in cands:
        real = contained(bundle, c)
        if real and os.path.isfile(real):
            return real, how
    base = os.path.basename(ref.replace("\\", "/")).lower()
    hits = [h for h in (index or _bundle_index(bundle)).get(base, []) if contained(bundle, h)]
    if len(hits) == 1:
        return os.path.realpath(hits[0]), "unique file name in the bundle"
    if len(hits) > 1:
        return None, f"{len(hits)} files named {base} in the bundle"
    return None, f"not found ({how})" if how.startswith("package") else "not found"


class _Resolver:
    """trimesh resolver that only serves files inside one folder (glTF buffers, OBJ materials)."""

    def __init__(self, folder: str, bundle: str):
        self.folder, self.bundle = folder, bundle

    def get(self, name):
        p = contained(self.bundle, os.path.join(self.folder, *str(name).replace("\\", "/").split("/")))
        if not p or not os.path.isfile(p) or os.path.getsize(p) > LIMITS["zip_file"]:
            raise FileNotFoundError(name)
        with open(p, "rb") as fh:
            return fh.read()

    def namespaced(self, namespace):
        return _Resolver(os.path.join(self.folder, namespace), self.bundle)

    def write(self, name, data):
        raise PermissionError("read only")


def stl_faces(path: str) -> int | None:
    """Triangle count from a binary STL header when the size matches it, else None (ASCII or malformed)."""
    size = os.path.getsize(path)
    if size < 84:
        return None
    with open(path, "rb") as fh:
        head = fh.read(84)
    n = int.from_bytes(head[80:84], "little")
    return n if 84 + 50 * n == size else None


def load_mesh(path: str, fmt: str, bundle: str | None = None):
    """A validated trimesh.Trimesh (face cap, malformed files refused with a plain message)."""
    import trimesh

    name = os.path.basename(path)
    cap = LIMITS["mesh_faces"]
    if fmt == "stl":
        n = stl_faces(path)
        if n is not None and n > cap:
            raise ImportFailed(f"{name} has {n:,} triangles; the limit is {cap:,}. Decimate it (Blender, MeshLab) and try again.")
        if n is None:
            with open(path, "rb") as fh:
                head = fh.read(512)
            if not head.lstrip().lower().startswith(b"solid"):
                raise ImportFailed(f"{name} is not a valid STL file (its size doesn't match its triangle count).")
    if fmt == "dae":
        try:
            import collada  # noqa: F401
        except ImportError:
            raise ImportFailed(f"{name} is a COLLADA (.dae) mesh, which needs the optional pycollada package; "
                               "export the meshes as STL instead.") from None
    if fmt == "step":
        return load_step(path)
    with open(path, "rb") as fh:
        data = fh.read()
    resolver = _Resolver(os.path.dirname(path), bundle or os.path.dirname(path))
    if fmt == "gltf":
        _check_gltf_uris(data, name)
    try:
        m = trimesh.load(io.BytesIO(data), file_type=fmt, force="mesh", resolver=resolver, process=False)
    except ImportFailed:
        raise
    except BaseException as exc:  # noqa: BLE001  loaders raise anything, even ModuleNotFoundError on bad STL
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ImportFailed(f"{name} is not a valid {fmt.upper()} mesh ({exc.__class__.__name__}).") from None
    if not isinstance(m, trimesh.Trimesh) or len(m.faces) == 0:
        raise ImportFailed(f"{name} has no triangles Morph can use.")
    if len(m.faces) > cap:
        raise ImportFailed(f"{name} has {len(m.faces):,} triangles; the limit is {cap:,}. Decimate it and try again.")
    if not np.all(np.isfinite(m.vertices)):
        raise ImportFailed(f"{name} has invalid (NaN or infinite) vertex coordinates.")
    return m


def _check_gltf_uris(data: bytes, name: str):
    import json

    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImportFailed(f"{name} is not valid glTF JSON.") from None
    for key in ("buffers", "images"):
        for b in doc.get(key) or []:
            uri = (b or {}).get("uri") or ""
            if uri.startswith("data:"):
                continue
            if "://" in uri or uri.startswith("/") or ".." in uri.replace("\\", "/").split("/") or ":" in uri:
                raise ImportFailed(f"{name} references {uri[:60]!r} outside its folder; export a .glb instead.")


def load_step(path: str):
    """STEP/IGES through the optional cascadio extra (MIT); output is metres, returned in mm."""
    import tempfile

    import trimesh

    try:
        import cascadio
    except ImportError:
        raise ImportFailed(f"{os.path.basename(path)} is a STEP file, which needs the optional CAD extra: "
                           'pip install "morph[cad]". Or export an STL.') from None
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "part.glb")
        try:
            cascadio.step_to_glb(path, out, 0.1, 0.5)
            m = trimesh.load(out, force="mesh")
        except Exception as exc:  # noqa: BLE001
            raise ImportFailed(f"{os.path.basename(path)} could not be read as STEP ({exc.__class__.__name__}); "
                               "an assembly STEP that references other files needs to be exported as one file.") from None
    if not isinstance(m, trimesh.Trimesh) or len(m.faces) == 0:
        raise ImportFailed(f"{os.path.basename(path)} has no solid geometry (an assembly that references other "
                           "files? Export a single STEP).")
    if len(m.faces) > LIMITS["mesh_faces"]:
        raise ImportFailed(f"{os.path.basename(path)} tessellates to {len(m.faces):,} triangles; the limit is "
                           f"{LIMITS['mesh_faces']:,}.")
    m.apply_scale(1000.0)
    return m


def _unique(name: str, taken: set) -> str:
    out, k = name, 2
    while out in taken:
        out, k = f"{name}_{k}", k + 1
    taken.add(out)
    return out


def normalize(urdf_path: str, bundle: str, out_dir: str, text: str | None = None, source_name: str | None = None,
              filename: str = "source.urdf", mesh_scale: float | None = None) -> dict:
    """Write out_dir/<filename> + out_dir/meshes/ from a URDF inside `bundle`. Raises ImportFailed when the
    file isn't a usable robot; missing meshes are reported (the robot still loads without them)."""
    from .detect import sniff

    bundle = os.path.realpath(bundle)
    urdf_dir = os.path.dirname(os.path.realpath(urdf_path))
    label = source_name or os.path.basename(urdf_path)
    root_el = parse_xml_text(text, label) if text is not None else parse_xml(urdf_path)
    if root_el.tag != "robot":
        raise ImportFailed(f"{label} is not a URDF (its root element is <{root_el.tag}>).")
    warnings, renamed = [], {}
    links = root_el.findall("link")
    joints = root_el.findall("joint")
    if not links:
        raise ImportFailed(f"{label} has no links.")
    if len(links) > 400 or len(joints) > 400:
        raise ImportFailed(f"{label} has {len(links)} links; Morph imports single arms (at most a few dozen links).")

    # ---- names: ASCII ids, unique, reserved names renamed
    taken, lmap = set(), {}
    for l in links:
        n = l.get("name") or ""
        s = safe_name(n)
        s = RESERVED.get(s, s)
        s = _unique(s, taken)
        if n in lmap:
            raise ImportFailed(f"{label} defines the link {n!r} twice.")
        lmap[n] = s
        if s != n:
            renamed[n] = s
    jtaken, jmap = set(), {}
    for j in joints:
        n = j.get("name") or ""
        s = _unique(RESERVED.get(safe_name(n), safe_name(n)), jtaken | {"tcp_fixed", "morph_base_fixed"})
        jtaken.add(s)
        jmap[n] = s
        if s != n:
            renamed[n] = s
    if "world" in lmap and lmap["world"] != "world":
        warnings.append('renamed the link "world" to "world_link" (MuJoCo reserves that name)')

    # ---- tree checks
    kids, parent_of = {}, {}
    for j in joints:
        p, c = j.find("parent"), j.find("child")
        if p is None or c is None or p.get("link") not in lmap or c.get("link") not in lmap:
            raise ImportFailed(f"{label}: joint {j.get('name')!r} connects a link that doesn't exist.")
        ch = c.get("link")
        if ch in parent_of:
            raise ImportFailed(f"{label}: link {ch!r} has two parent joints; a URDF must be a tree.")
        parent_of[ch] = p.get("link")
        kids.setdefault(p.get("link"), []).append(ch)
    roots = [l.get("name") for l in links if l.get("name") not in parent_of]
    if len(roots) != 1:
        raise ImportFailed(f"{label} has {len(roots)} root links ({', '.join(map(str, roots[:4]))}); "
                           "every link must hang off one base link.")
    seen, todo = set(), [roots[0]]
    while todo:
        n = todo.pop()
        if n in seen:
            raise ImportFailed(f"{label} has a loop of joints.")
        seen.add(n)
        todo += kids.get(n, [])
    if len(seen) != len(links):
        raise ImportFailed(f"{label}: {len(links) - len(seen)} links aren't connected to the base link.")

    # ---- meshes
    pkgs = packages(bundle)
    index = _bundle_index(bundle)
    mesh_dir = os.path.join(out_dir, "meshes")
    os.makedirs(mesh_dir, exist_ok=True)
    copied, missing, total, mtaken, refused, bad = {}, [], 0, set(), [], {}
    out_root = ET.Element("robot", {"name": root_el.get("name") or "robot"})
    for el in root_el:
        if el.tag not in KEEP:
            continue
        if el.tag == "material":
            m = ET.SubElement(out_root, "material", {"name": el.get("name") or ""})
            c = el.find("color")
            if c is not None:
                ET.SubElement(m, "color", {"rgba": c.get("rgba", "0.8 0.8 0.8 1")})
            continue
        if el.tag == "joint":
            jt = el.get("type") or "fixed"
            if jt in ("floating", "planar"):
                warnings.append(f"joint {el.get('name')} is {jt}; imported as fixed")
                jt = "fixed"
            if jt not in ("revolute", "continuous", "prismatic", "fixed"):
                raise ImportFailed(f"{label}: joint {el.get('name')!r} has an unknown type {jt!r}.")
            j = ET.SubElement(out_root, "joint", {"name": jmap[el.get("name") or ""], "type": jt})
            ET.SubElement(j, "parent", {"link": lmap[el.find("parent").get("link")]})
            ET.SubElement(j, "child", {"link": lmap[el.find("child").get("link")]})
            for tag in ("origin", "axis", "limit"):
                src = el.find(tag)
                if src is not None:
                    ET.SubElement(j, tag, {k: v for k, v in src.attrib.items() if k in
                                           ("xyz", "rpy", "lower", "upper", "effort", "velocity")})
            mim = el.find("mimic")
            if mim is not None and mim.get("joint") in jmap:
                ET.SubElement(j, "mimic", {"joint": jmap[mim.get("joint")], "multiplier": mim.get("multiplier", "1"),
                                           "offset": mim.get("offset", "0")})
            continue
        # link
        lk = ET.SubElement(out_root, "link", {"name": lmap[el.get("name") or ""]})
        visuals = el.findall("visual") or el.findall("collision")[:1]
        if not el.findall("visual") and visuals:
            warnings.append(f"link {el.get('name')} has no visual; used its collision shape")
        for vis in visuals:
            g = vis.find("geometry")
            if g is None:
                continue
            v = ET.SubElement(lk, "visual")
            if vis.find("origin") is not None:
                ET.SubElement(v, "origin", {k: vv for k, vv in vis.find("origin").attrib.items() if k in ("xyz", "rpy")})
            gg = ET.SubElement(v, "geometry")
            mesh = g.find("mesh")
            if mesh is not None:
                ref = mesh.get("filename") or ""
                total += 1
                real, how = resolve_mesh(ref, urdf_dir, bundle, pkgs, index)
                if real is not None and real in bad:             # already refused for another visual
                    real, how = None, bad[real]
                if real is None:
                    missing.append({"ref": ref, "why": how, "link": el.get("name")})
                    lk.remove(v)
                    continue
                if real not in copied:
                    fmt = sniff(real)
                    base = safe_name(os.path.splitext(os.path.basename(real))[0])
                    fn = _unique(base, mtaken) + ".stl"
                    dst = os.path.join(mesh_dir, fn)
                    try:
                        nf = stl_faces(real) if fmt in MESH_OK else None
                        if nf is not None:
                            if nf > LIMITS["mesh_faces"]:
                                raise ImportFailed(f"{os.path.basename(real)} has {nf:,} triangles; the limit is "
                                                   f"{LIMITS['mesh_faces']:,}. Decimate it and try again.")
                            if nf == 0:
                                raise ImportFailed(f"{os.path.basename(real)} has no triangles.")
                            shutil.copyfile(real, dst)
                        elif fmt in MESH_OK:
                            load_mesh(real, fmt, bundle).export(dst, file_type="stl")
                        elif fmt in MESH_CONVERT:
                            load_mesh(real, fmt, bundle).export(dst, file_type="stl")
                        else:
                            raise ImportFailed(f"{os.path.basename(real)} is not a mesh Morph can read ({fmt}).")
                    except ImportFailed as exc:
                        bad[real] = str(exc)
                        refused.append(str(exc))
                        missing.append({"ref": ref, "why": str(exc), "link": el.get("name")})
                        mtaken.discard(fn[:-4])
                        lk.remove(v)
                        continue
                    copied[real] = fn
                attrs = {"filename": f"meshes/{copied[real]}"}
                if mesh.get("scale") or mesh_scale:
                    try:
                        sc = [float(x) for x in (mesh.get("scale") or "1 1 1").split()][:3] or [1.0, 1.0, 1.0]
                    except ValueError:
                        sc = [1.0, 1.0, 1.0]
                        warnings.append(f"ignored the unreadable mesh scale {mesh.get('scale')[:40]!r} of {ref[-60:]}")
                    sc = (sc * 3)[:3] if len(sc) < 3 else sc
                    attrs["scale"] = " ".join(f"{x * (mesh_scale or 1.0):.6g}" for x in sc)
                ET.SubElement(gg, "mesh", attrs)
            else:
                prim = next((g.find(t) for t in ("box", "cylinder", "sphere") if g.find(t) is not None), None)
                if prim is None:
                    lk.remove(v)
                    continue
                ET.SubElement(gg, prim.tag, dict(prim.attrib))
            mat = vis.find("material")
            if mat is not None:
                me = ET.SubElement(v, "material", {"name": mat.get("name") or ""})
                c = mat.find("color")
                if c is not None:
                    ET.SubElement(me, "color", {"rgba": c.get("rgba", "0.8 0.8 0.8 1")})
    out = os.path.join(out_dir, filename)
    _write(out_root, out)
    if missing:
        names = ", ".join(os.path.basename(m["ref"]) for m in missing[:3]) + ("…" if len(missing) > 3 else "")
        searched = ", ".join(sorted(pkgs)) or "no package.xml found"
        warnings.insert(0, f"{len(missing)} of {total} meshes missing ({names}); searched packages: {searched}. "
                           "Drop the folder that contains package.xml and the meshes.")
    movable = [j for j in out_root.findall("joint") if j.get("type") != "fixed"]
    return {"urdf": out, "name": root_el.get("name") or "", "root": lmap[roots[0]], "links": len(links),
            "joints": len(joints), "movable": len(movable),
            "meshes": {"total": total, "found": total - len(missing), "missing": missing,
                       "searched": sorted(pkgs)},
            "renamed": renamed, "warnings": warnings, "packages": {k: os.path.relpath(v, bundle) for k, v in pkgs.items()}}


def _write(root_el, path):
    ET.indent(root_el, space="  ") if hasattr(ET, "indent") else None
    data = ET.tostring(root_el, encoding="unicode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0"?>\n' + data + "\n")


def _fmt(v) -> str:
    return " ".join(f"{float(x):.6g}" for x in v)


def finalize(urdf_in: str, urdf_out: str, yaw: float = 0.0, lift_mm: float = 0.0, tcp: dict | None = None) -> str:
    """Add the morph_base root (yaw about z, lift so the base sits on z = 0) and a `tcp` link.
    tcp: {"link": hand link, "T": 4x4 (mm) hand -> tcp}."""
    import trimesh

    root_el = parse_xml(urdf_in)
    for el in [e for e in root_el if e.tag in ("link", "joint") and e.get("name") in ("morph_base", "morph_base_fixed",
                                                                                          "tcp", "tcp_fixed")]:
        root_el.remove(el)
    children = {j.find("child").get("link") for j in root_el.findall("joint")}
    old_root = next(l.get("name") for l in root_el.findall("link") if l.get("name") not in children)
    root_el.insert(0, ET.Element("link", {"name": "morph_base"}))
    j = ET.SubElement(root_el, "joint", {"name": "morph_base_fixed", "type": "fixed"})
    ET.SubElement(j, "parent", {"link": "morph_base"})
    ET.SubElement(j, "child", {"link": old_root})
    ET.SubElement(j, "origin", {"xyz": f"0 0 {lift_mm / 1000.0:.6f}", "rpy": f"0 0 {yaw:.6f}"})
    if tcp:
        T = np.asarray(tcp["T"], float)
        r, p, y = trimesh.transformations.euler_from_matrix(T, "sxyz")
        ET.SubElement(root_el, "link", {"name": "tcp"})
        jt = ET.SubElement(root_el, "joint", {"name": "tcp_fixed", "type": "fixed"})
        ET.SubElement(jt, "parent", {"link": tcp["link"]})
        ET.SubElement(jt, "child", {"link": "tcp"})
        ET.SubElement(jt, "origin", {"xyz": _fmt(T[:3, 3] / 1000.0), "rpy": _fmt((r, p, y))})
    _write(root_el, urdf_out)
    return urdf_out


def joints_of(urdf_path: str) -> dict:
    """{name: {name, type, parent, child, lower, upper, effort, mimic, axis}} from a normalized URDF."""
    root_el = parse_xml(urdf_path)
    out = {}
    for j in root_el.findall("joint"):
        lim = j.find("limit")
        mim = j.find("mimic")
        ax = j.find("axis")
        jt = j.get("type")
        lo = float(lim.get("lower", -math.pi)) if lim is not None and lim.get("lower") else -math.pi
        hi = float(lim.get("upper", math.pi)) if lim is not None and lim.get("upper") else math.pi
        if jt == "continuous":
            lo, hi = -math.pi, math.pi
        out[j.get("name")] = {"name": j.get("name"), "type": jt, "parent": j.find("parent").get("link"),
                              "child": j.find("child").get("link"), "lower": lo, "upper": hi,
                              "effort": float(lim.get("effort", 0) or 0) if lim is not None else 0.0,
                              "mimic": mim.get("joint") if mim is not None else None,
                              "axis": [float(x) for x in (ax.get("xyz") if ax is not None else "1 0 0").split()][:3]}
    return out


def mesh_names(urdf_path: str) -> dict:
    """{link: "mesh file names joined"} for actuator and motor-part matching."""
    root_el = parse_xml(urdf_path)
    return {l.get("name"): " ".join(os.path.splitext(os.path.basename(m.get("filename", "")))[0]
                                    for m in l.iter("mesh")) for l in root_el.findall("link")}
