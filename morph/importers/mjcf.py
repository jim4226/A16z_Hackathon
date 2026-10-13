"""MuJoCo XML (MJCF) robots -> a URDF bundle that the imported-robot pipeline loads like any URDF.

  rep = to_urdf("menagerie/trs_so_arm100/so_arm100.xml", bundle="menagerie", out_dir="work/cand0")

MuJoCo compiles the model (includes, defaults, mesh scaling), then every body becomes a link, its first hinge
or slide joint the URDF joint (anchor offsets folded into the frame), each visual geom one STL part in mm
(mesh name kept, so "Base_Motor" stays recognisable as a motor), joint equality constraints become mimic
joints and tool-like sites become fixed tool frames. A scene with a floor and props keeps only the body tree
with the most joints (the robot).

Before MuJoCo sees the file, every file reference (include, mesh, texture, hfield, skin, model, and the
compiler's meshdir/texturedir/assetdir) must resolve inside the bundle, and DOCTYPE/ENTITY are refused.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

import numpy as np

from .common import LIMITS, ImportFailed, contained, parse_xml
from .urdf import _unique, _write, safe_name, stl_faces

FILE_ATTRS = re.compile(r"^(file|file(right|left|up|down|front|back)|meshdir|texturedir|assetdir|content_type)$")
TOOL_SITE = re.compile(r"(^|_)(tcp|tool0?|ee|eef|end_?effector|grasp|pinch|gripper_site|attachment_site)(_|$|site)", re.I)


def check_refs(path: str, bundle: str, seen: set | None = None, depth: int = 0) -> list:
    """Every file an MJCF (and its includes) can read must be inside the bundle. Returns the files checked."""
    seen = set() if seen is None else seen
    real = contained(bundle, path)
    if real is None:
        raise ImportFailed(f"{os.path.basename(path)} is outside the imported folder.")
    if real in seen:
        return []
    if depth > 10:
        raise ImportFailed("The MuJoCo model includes files more than 10 levels deep.")
    seen.add(real)
    root = parse_xml(real)
    here = os.path.dirname(real)
    dirs = {}
    for el in root.iter("compiler"):
        for k in ("meshdir", "texturedir", "assetdir"):
            if el.get(k) is not None:
                d = el.get(k)
                if os.path.isabs(d) or contained(bundle, os.path.join(here, d)) is None:
                    raise ImportFailed(f"{os.path.basename(path)}: compiler {k}={d!r} points outside the imported folder.")
                dirs[k] = d
    out = [real]
    for el in root.iter():
        for k, v in el.attrib.items():
            if not k.startswith("file") or not v:
                continue
            if os.path.isabs(v) or "://" in v:
                raise ImportFailed(f"{os.path.basename(path)} references {v[:60]!r}; only files inside the folder are imported.")
            base = here
            if el.tag == "mesh" or el.tag == "skin":
                base = os.path.join(here, dirs.get("meshdir") or dirs.get("assetdir") or "")
            elif el.tag in ("texture", "hfield"):
                base = os.path.join(here, dirs.get("texturedir") or dirs.get("assetdir") or "")
            p = os.path.join(base, *v.replace("\\", "/").split("/"))
            r = contained(bundle, p)
            if r is None:
                raise ImportFailed(f"{os.path.basename(path)} references {v[:60]!r} outside the imported folder.")
            if el.tag in ("include", "model"):
                if not os.path.isfile(r):
                    raise ImportFailed(f"{os.path.basename(path)} includes {v!r}, which is missing.")
                out += check_refs(r, bundle, seen, depth + 1)
            elif el.tag == "mesh" and os.path.isfile(r) and r.lower().endswith(".stl"):
                n = stl_faces(r)
                if n is not None and n > LIMITS["mesh_faces"]:
                    raise ImportFailed(f"{v} has {n:,} triangles; the limit is {LIMITS['mesh_faces']:,}.")
    return out


def includes_of(path: str) -> set:
    """Real paths of the files an MJCF includes (a scene.xml that includes the robot is not a second robot)."""
    try:
        root = parse_xml(path)
    except ImportFailed:
        return set()
    here = os.path.dirname(os.path.realpath(path))
    return {os.path.realpath(os.path.join(here, el.get("file"))) for el in root.iter("include") if el.get("file")}


def _T(pos, quat):
    import mujoco

    R = np.zeros(9)
    mujoco.mju_quat2Mat(R, np.asarray(quat, float))
    M = np.eye(4)
    M[:3, :3] = R.reshape(3, 3)
    M[:3, 3] = np.asarray(pos, float) * 1000.0
    return M


def _rpy(M):
    import trimesh
    return trimesh.transformations.euler_from_matrix(M, "sxyz")


def _origin(parent, M):
    r, p, y = _rpy(M)
    xyz = M[:3, 3] / 1000.0
    ET.SubElement(parent, "origin", {"xyz": f"{xyz[0]:.6g} {xyz[1]:.6g} {xyz[2]:.6g}", "rpy": f"{r:.6g} {p:.6g} {y:.6g}"})


def to_urdf(path: str, bundle: str, out_dir: str, filename: str = "source.urdf") -> dict:
    """Compile an MJCF with MuJoCo and write out_dir/<filename> + out_dir/meshes/*.stl (mm, scale 0.001)."""
    import trimesh

    try:
        import mujoco
    except ImportError:  # mujoco is a core dependency; this only happens in a broken install
        raise ImportFailed("MuJoCo isn't installed, so MuJoCo XML can't be imported.") from None
    check_refs(path, bundle)
    try:
        m = mujoco.MjModel.from_xml_path(os.path.realpath(path))
    except Exception as exc:  # noqa: BLE001  (mujoco raises ValueError with the parser's message)
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        raise ImportFailed(f"MuJoCo couldn't load {os.path.basename(path)}: {msg[:200]}") from None
    obj = mujoco.mjtObj
    nm = lambda t, i: mujoco.mj_id2name(m, t, i) or ""  # noqa: E731
    kids = {b: [] for b in range(m.nbody)}
    for b in range(1, m.nbody):
        kids[int(m.body_parentid[b])].append(b)

    def sub(b):
        out, todo = [], [b]
        while todo:
            n = todo.pop()
            out.append(n)
            todo += kids[n]
        return out

    jtypes = (int(mujoco.mjtJoint.mjJNT_HINGE), int(mujoco.mjtJoint.mjJNT_SLIDE))
    body_joints = {b: [j for j in range(m.njnt) if int(m.jnt_bodyid[j]) == b] for b in range(m.nbody)}

    def njoint(b):
        return sum(1 for x in sub(b) for j in body_joints[x] if int(m.jnt_type[j]) in jtypes)

    roots = kids[0]
    if not roots:
        raise ImportFailed(f"{os.path.basename(path)} has no bodies.")
    robot = max(roots, key=njoint)
    notes = []
    others = [nm(obj.mjOBJ_BODY, b) or f"body {b}" for b in roots if b != robot]
    if others:
        notes.append(f"kept the robot body tree; ignored {len(others)} other bodies ({', '.join(others[:3])})")
    if njoint(robot) == 0:
        raise ImportFailed(f"{os.path.basename(path)} has no hinge or slide joints; Morph imports arms.")

    taken, lname = {"mjcf_world"}, {}
    for b in sub(robot):
        s = safe_name(nm(obj.mjOBJ_BODY, b) or f"body{b}")
        lname[b] = _unique({"world": "world_link", "tcp": "tcp_orig", "morph_base": "morph_base_orig"}.get(s, s), taken)
    jtaken, mtaken = set(), set()
    mesh_dir = os.path.join(out_dir, "meshes")
    os.makedirs(mesh_dir, exist_ok=True)
    out = ET.Element("robot", {"name": safe_name(nm(obj.mjOBJ_MODEL, 0) or os.path.splitext(os.path.basename(path))[0])})
    ET.SubElement(out, "link", {"name": "mjcf_world"})
    jname_of = {}
    n_parts, total_faces = 0, 0
    for b in sub(robot):
        link = ET.SubElement(out, "link", {"name": lname[b]})
        parent = "mjcf_world" if b == robot else lname[int(m.body_parentid[b])]
        Tb = _T(m.body_pos[b], m.body_quat[b])
        js = [j for j in body_joints[b] if int(m.jnt_type[j]) in jtypes]
        other = [j for j in body_joints[b] if int(m.jnt_type[j]) not in jtypes]
        if other:
            notes.append(f"{lname[b]}: a free or ball joint is welded")
        if len(js) > 1:
            notes.append(f"{lname[b]}: {len(js)} joints on one body; kept the first")
        anchor = np.eye(4)
        if js:
            j = js[0]
            jp = np.asarray(m.jnt_pos[j], float) * 1000.0
            if np.linalg.norm(jp) > 1e-6:
                anchor[:3, 3] = jp
            hinge = int(m.jnt_type[j]) == int(mujoco.mjtJoint.mjJNT_HINGE)
            jn = _unique(safe_name(nm(obj.mjOBJ_JOINT, j) or f"{lname[b]}_joint"), jtaken)
            jname_of[j] = jn
            limited = bool(m.jnt_limited[j])
            jt = "revolute" if hinge and limited else ("continuous" if hinge else "prismatic")
            je = ET.SubElement(out, "joint", {"name": jn, "type": jt})
            ET.SubElement(je, "parent", {"link": parent})
            ET.SubElement(je, "child", {"link": lname[b]})
            _origin(je, Tb @ anchor)
            ax = m.jnt_axis[j]
            ET.SubElement(je, "axis", {"xyz": f"{ax[0]:.6g} {ax[1]:.6g} {ax[2]:.6g}"})
            lo, hi = (float(m.jnt_range[j][0]), float(m.jnt_range[j][1])) if limited else (-0.1, 0.1)
            if jt != "continuous":
                ET.SubElement(je, "limit", {"lower": f"{lo:.6g}", "upper": f"{hi:.6g}", "effort": "0", "velocity": "3"})
        else:
            je = ET.SubElement(out, "joint", {"name": _unique(f"{lname[b]}_fixed", jtaken), "type": "fixed"})
            ET.SubElement(je, "parent", {"link": parent})
            ET.SubElement(je, "child", {"link": lname[b]})
            _origin(je, Tb)
        inv_anchor = np.linalg.inv(anchor)
        gids = [g for g in range(m.ngeom) if int(m.geom_bodyid[g]) == b and int(m.geom_type[g]) != int(mujoco.mjtGeom.mjGEOM_PLANE)]
        vis = [g for g in gids if int(m.geom_contype[g]) == 0 and int(m.geom_conaffinity[g]) == 0] or gids
        for g in vis:
            gt = int(m.geom_type[g])
            size = np.asarray(m.geom_size[g], float) * 1000.0
            G = mujoco.mjtGeom
            if gt == int(G.mjGEOM_MESH):
                mid = int(m.geom_dataid[g])
                va, vn = int(m.mesh_vertadr[mid]), int(m.mesh_vertnum[mid])
                fa, fn = int(m.mesh_faceadr[mid]), int(m.mesh_facenum[mid])
                mesh = trimesh.Trimesh(np.asarray(m.mesh_vert[va:va + vn], float) * 1000.0, m.mesh_face[fa:fa + fn], process=False)
                pname = safe_name(nm(obj.mjOBJ_MESH, mid) or f"{lname[b]}_mesh{g}")
            elif gt == int(G.mjGEOM_BOX):
                mesh, pname = trimesh.creation.box(size * 2), f"{lname[b]}_box"
            elif gt == int(G.mjGEOM_CYLINDER):
                mesh, pname = trimesh.creation.cylinder(size[0], size[1] * 2), f"{lname[b]}_cylinder"
            elif gt == int(G.mjGEOM_SPHERE):
                mesh, pname = trimesh.creation.icosphere(radius=size[0]), f"{lname[b]}_sphere"
            elif gt == int(G.mjGEOM_CAPSULE):
                mesh, pname = trimesh.creation.capsule(size[1] * 2, size[0]), f"{lname[b]}_capsule"
            elif gt == int(G.mjGEOM_ELLIPSOID):
                mesh, pname = trimesh.creation.icosphere(subdivisions=2).apply_scale(size), f"{lname[b]}_ellipsoid"
            else:
                continue
            total_faces += len(mesh.faces)
            if total_faces > 3 * LIMITS["mesh_faces"]:
                raise ImportFailed("The model's meshes add up to more than 4.5 million triangles; simplify them first.")
            mesh.apply_transform(inv_anchor @ _T(m.geom_pos[g], m.geom_quat[g]))
            fname = _unique(pname, mtaken)
            mesh.export(os.path.join(mesh_dir, fname + ".stl"), file_type="stl")
            v = ET.SubElement(link, "visual")
            gg = ET.SubElement(v, "geometry")
            ET.SubElement(gg, "mesh", {"filename": f"meshes/{fname}.stl", "scale": "0.001 0.001 0.001"})
            rgba = m.geom_rgba[g]
            if int(m.geom_matid[g]) >= 0:
                rgba = m.mat_rgba[int(m.geom_matid[g])]
            mat = ET.SubElement(v, "material", {"name": f"c_{fname}"})
            ET.SubElement(mat, "color", {"rgba": " ".join(f"{float(c):.3g}" for c in rgba)})
            n_parts += 1
        for s in range(m.nsite):
            if int(m.site_bodyid[s]) == b and TOOL_SITE.search(nm(obj.mjOBJ_SITE, s) or ""):
                sl = _unique(safe_name(nm(obj.mjOBJ_SITE, s)), taken)
                ET.SubElement(out, "link", {"name": sl})
                se = ET.SubElement(out, "joint", {"name": _unique(f"{sl}_fixed", jtaken), "type": "fixed"})
                ET.SubElement(se, "parent", {"link": lname[b]})
                ET.SubElement(se, "child", {"link": sl})
                _origin(se, inv_anchor @ _T(m.site_pos[s], m.site_quat[s]))
    # joint equality constraints -> mimic (follower = c0 + c1 * leader)
    for e in range(m.neq):
        if int(m.eq_type[e]) != int(mujoco.mjtEq.mjEQ_JOINT):
            continue
        a, bb = int(m.eq_obj1id[e]), int(m.eq_obj2id[e])
        if a in jname_of and bb in jname_of:
            for je in out.findall("joint"):
                if je.get("name") == jname_of[a]:
                    c = m.eq_data[e]
                    ET.SubElement(je, "mimic", {"joint": jname_of[bb], "multiplier": f"{float(c[1]):.6g}",
                                                "offset": f"{float(c[0]):.6g}"})
    if n_parts == 0:
        raise ImportFailed(f"{os.path.basename(path)} has no visual geometry Morph can use.")
    dst = os.path.join(out_dir, filename)
    _write(out, dst)
    actuated = []
    for a in range(m.nu):
        if int(m.actuator_trntype[a]) == int(mujoco.mjtTrn.mjTRN_JOINT) and int(m.actuator_trnid[a][0]) in jname_of:
            actuated.append({"joint": jname_of[int(m.actuator_trnid[a][0])], "actuator": nm(obj.mjOBJ_ACTUATOR, a)})
    movable = sum(1 for je in out.findall("joint") if je.get("type") != "fixed")
    return {"urdf": dst, "name": out.get("name"), "root": "mjcf_world", "links": len(out.findall("link")),
            "joints": len(out.findall("joint")), "movable": movable,
            "meshes": {"total": n_parts, "found": n_parts, "missing": [], "searched": []},
            "renamed": {}, "warnings": notes, "actuated": actuated}
