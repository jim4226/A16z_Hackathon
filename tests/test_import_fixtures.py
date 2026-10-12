"""Small generated fixtures for the import tests (no downloads, no network)."""

from __future__ import annotations

import io
import json
import os
import zipfile

import numpy as np
import trimesh

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SO101 = os.path.join(ROOT, "robots", "so101")


def stl(path, extents=(10, 10, 10), center=(0, 0, 0)):
    m = trimesh.creation.box(extents)
    m.apply_translation(center)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    m.export(path, file_type="stl")
    return path


ARM = [  # link, mesh center (m), size (m), joint to it: (name, type, parent, origin xyz, axis, lower, upper)
    ("base_link", (0, 0, 0.02), (0.08, 0.08, 0.04), None),
    ("shoulder", (0, 0, 0.02), (0.04, 0.04, 0.04), ("shoulder_pan", "revolute", "base_link", (0, 0, 0.04), (0, 0, 1), -2.0, 2.0)),
    ("upper_arm", (0.06, 0, 0), (0.12, 0.03, 0.03), ("shoulder_lift", "revolute", "shoulder", (0, 0, 0.04), (0, 1, 0), -1.6, 1.6)),
    ("lower_arm", (0.05, 0, 0), (0.10, 0.025, 0.025), ("elbow_flex", "revolute", "upper_arm", (0.12, 0, 0), (0, 1, 0), -2.5, 2.5)),
    ("wrist", (0.02, 0, 0), (0.04, 0.025, 0.025), ("wrist_flex", "revolute", "lower_arm", (0.10, 0, 0), (0, 1, 0), -1.8, 1.8)),
    ("hand", (0.03, 0.008, 0), (0.06, 0.006, 0.02), ("wrist_roll", "revolute", "wrist", (0.04, 0, 0), (1, 0, 0), -2.8, 2.8)),
    ("jaw", (0.025, 0, 0), (0.05, 0.006, 0.02), ("gripper", "revolute", "hand", (0.01, -0.012, 0), (0, 0, 1), 0.0, 1.0)),
]


def arm_urdf(folder, motor_names=False, package=None, world=False, joint_names=None, missing=(), name="testarm"):
    """A 5-DOF arm + revolute jaw made of boxes, with STL meshes in <folder>/meshes (package:// paths if package)."""
    os.makedirs(os.path.join(folder, "meshes"), exist_ok=True)
    L = [f'<?xml version="1.0"?>\n<robot name="{name}">']
    if world:
        L.append('<link name="world"/><joint name="world_fixed" type="fixed"><parent link="world"/><child link="base_link"/></joint>')
    rename = joint_names or {}
    for link, c, size, joint in ARM:
        mesh = f"{link}_sts3215" if motor_names else link
        if link not in missing:
            stl(os.path.join(folder, "meshes", mesh + ".stl"), np.array(size), np.array(c))
        ref = f"package://{package}/meshes/{mesh}.stl" if package else f"meshes/{mesh}.stl"
        L.append(f'<link name="{link}"><visual><geometry><mesh filename="{ref}" scale="1 1 1"/></geometry>'
                 f'<material name="m_{link}"><color rgba="0.8 0.7 0.2 1"/></material></visual></link>')
        if joint:
            jn, jt, parent, xyz, axis, lo, hi = joint
            jn = rename.get(jn, jn)
            L.append(f'<joint name="{jn}" type="{jt}"><parent link="{parent}"/><child link="{link}"/>'
                     f'<origin xyz="{xyz[0]} {xyz[1]} {xyz[2]}" rpy="0 0 0"/><axis xyz="{axis[0]} {axis[1]} {axis[2]}"/>'
                     f'<limit lower="{lo}" upper="{hi}" effort="35" velocity="3"/></joint>')
    L.append("</robot>")
    if package:
        with open(os.path.join(folder, "package.xml"), "w", encoding="utf-8") as fh:
            fh.write(f'<?xml version="1.0"?><package format="3"><name>{package}</name><license>Apache-2.0</license></package>')
    p = os.path.join(folder, f"{name}.urdf")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return p


def so101_copy(folder):
    """The shipped SO-101 URDF and meshes without its hand-written profile (auto-profiling from the URDF alone)."""
    import shutil

    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(os.path.join(SO101, "so101_new_calib.urdf"), os.path.join(folder, "so101_new_calib.urdf"))
    shutil.copytree(os.path.join(SO101, "assets"), os.path.join(folder, "assets"))
    shutil.copyfile(os.path.join(SO101, "LICENSE"), os.path.join(folder, "LICENSE"))
    return folder


def egg_stl(path, metres=True):
    m = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    m.apply_scale((22, 22, 28.5))
    if metres:
        m.apply_scale(0.001)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    m.export(path, file_type="stl")
    return path


BOM_MD = """# Robot

#### Parts For One Follower Arm:

| Part                                        | Amount | Unit Cost (US) | Buy US                                                     | Unit Cost (EU) | Buy EU |
| ------------------------------------------- | ------ | -------------- | ---------------------------------------------------------- | -------------- | ------ |
| STS3215 Servo 7.4V, 1/345 gear (C001)    | 6      | $13.89            | [Alibaba](https://www.alibaba.com/product-detail/STS3215_1) | €12.2 | [Alibaba](https://www.alibaba.com/x) |
| Motor Control Board                         | 1      | $10.6            | [Amazon](https://www.amazon.com/dp/B0CTMM4LWK/)              | €11.4 | [Amazon](https://www.amazon.fr/dp/B0CJ6TP3TP/) |
| USB-C Cable 2 pcs                           | 1      | $7             | [Amazon](https://www.amazon.com/dp/B0B8NWLLW2/)               | €7    | [Amazon](https://www.amazon.fr/dp/B07BNF842T/) |
| Power Supply    | 1      | $10            | [Amazon](https://www.amazon.com/dp/B087LY41PV/)                     | €15.7 | [Amazon](https://www.amazon.fr/dp/B01HRR9GY4/) |
| Screwdriver Set<sup>[1](#myfootnote1)</sup> | 1      | $6             | [Amazon](https://www.amazon.com/dp/B0DB227RTH)                | €9    | [Amazon](https://www.amazon.fr/dp/B0BNQBNFFJ) |
| Total                                       | ---    | $121.94           | ---                                                    | €124.3 | --- |

Some text after the table.
"""


def xlsx(path, rows):
    """A minimal .xlsx (shared strings + inline numbers) written with the standard library."""
    strings, idx = [], {}

    def si(s):
        if s not in idx:
            idx[s] = len(strings)
            strings.append(s)
        return idx[s]

    def col(i):
        return chr(65 + i)

    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{col(c)}{r}"
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{si(str(v))}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook {ns} {rns}><sheets><sheet name="BOM" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0"?><worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    return path


KICAD_PCB = """(kicad_pcb (version 20240108) (generator "pcbnew")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (attr smd)
    (property "Reference" "R1" (at 0 0 0)) (property "Value" "10k" (at 0 0 0)) (property "MPN" "RC0603FR-0710KL" (at 0 0 0)))
  (footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (attr smd)
    (property "Reference" "R2" (at 0 0 0)) (property "Value" "10k" (at 0 0 0)) (property "MPN" "RC0603FR-0710KL" (at 0 0 0)))
  (footprint "MountingHole:MountingHole_3.2mm_M3" (layer "F.Cu") (attr exclude_from_pos_files exclude_from_bom)
    (property "Reference" "H1" (at 0 0 0)) (property "Value" "M3" (at 0 0 0)))
  (footprint "Capacitor_SMD:C_0603_1608Metric" (layer "F.Cu")
    (fp_text reference "C1" (at 0 0)) (fp_text value "100n" (at 0 0)))
  (gr_rect (start 10 10) (end 60 40) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def morph_export(path, design: dict, extra: dict | None = None, robot: dict | None = None, rev="r004"):
    """The single-revision export layout (export-manifest.json + design.json + results.json)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("export-manifest.json", json.dumps({"format_version": 1, "project": "egg-picker", "revision": rev}))
        z.writestr("design.json", json.dumps(design))
        z.writestr("results.json", json.dumps({"summary": {"feasible": True}, "note": "never trusted"}))
        for name, data in (extra or {}).items():
            if isinstance(data, zipfile.ZipInfo):
                continue
            z.writestr(name, data)
        for name, data in (robot or {}).items():
            z.writestr(name, data)
    return path


def zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()
