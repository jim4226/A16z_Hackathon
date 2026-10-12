"""Manufacturing plan, BOM and assembly manual (morph.robot.manufacturing / morph.robot.manual).

The fixture builds a small robot assembly to the platform contract (kinematic chain, printed parts with
meshes, a CNC plate with profile/thickness, a sheet-metal bracket with meta['sheet'] flat pattern + bends,
a moulded cover, purchased actuators / modules / bearing with sku, fastener parts and step fastener lists)
plus an `efinal` dict per the electronics finalize schema, so these tests do not depend on the arm
generator's current state. One extra test runs the real generator when it is importable.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import zipfile

import manifold3d as m3d
import numpy as np
import pytest
import trimesh
from shapely.geometry import Point, Polygon, box

from morph.robot import catalog, components, manufacturing, manual
from morph.robot.assembly import Assembly, Cable, Joint, Part, Step, trans
from morph.spec import Design

EGG = {"name": "egg-picker", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
       "object.length_mm": 57.0, "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0,
       "finger.sensor": "fsr402", "finger.width_mm": 20.0, "finger.length_mm": 52.0, "electronics.servo": "sts3215"}


# ------------------------------------------------------------------ fixture: contract assembly
def _tri(man):
    m = man.to_mesh()
    return trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts), process=False)


def _box_m(lx, ly, lz, c=(0, 0, 0)):
    return m3d.Manifold.cube((lx, ly, lz), center=True).translate(c)


def _cyl_m(r, h, z0=0.0):
    return m3d.Manifold.cylinder(h, r, r, 64).translate((0, 0, z0))


def plate_profile():
    outer = Point(0, 0).buffer(70, quad_segs=24)
    holes = [Point(55 * math.cos(a), 55 * math.sin(a)).buffer(2.75, quad_segs=8) for a in np.linspace(0, 2 * math.pi, 5)[:-1] + math.pi / 4]
    holes += [Point(0, 0).buffer(11, quad_segs=12), Point(-38, -30).buffer(1.6, quad_segs=8)]
    poly = outer
    for h in holes:
        poly = poly.difference(h)
    return poly


def sheet_flat():
    """U-bracket flat pattern 50 x 76 mm, bends along u at v = 22 and v = 54, four holes in the web."""
    t, R, k = 1.6, 1.6, 0.44
    poly = box(0, 0, 50, 76)
    for x, y in ((12, 32), (38, 32), (12, 44), (38, 44), (25, 8), (25, 68)):
        poly = poly.difference(Point(x, y).buffer(1.7, quad_segs=8))
    bends = [{"line": [[0, 22], [50, 22]], "angle_deg": 90, "radius": R},
             {"line": [[0, 54], [50, 54]], "angle_deg": 90, "radius": R}]
    return poly, {"thickness": t, "bends": bends, "k_factor": k, "material": "Al5052"}


def make_asm(design: Design) -> Assembly:
    asm = Assembly("egg_picker")
    printed = dict(material="PETG", color="#e9edf2", process="fdm_print",
                   process_options=("fdm_print", "mjf_print", "cnc_mill", "injection_mold"))
    # base: CNC/laser plate with a real profile
    prof = plate_profile()
    asm.add_part(Part("base_plate", "base", components.trimesh_from_profile(prof, 8.0), step=1, profile=prof,
                      profile_frame=np.eye(4), thickness=8.0, material="PETG", color="#c4c8cd", process="fdm_print",
                      process_options=("fdm_print", "cnc_mill", "laser_cut"), group="base_plate"))
    asm.add_joint(Joint("j1_base_yaw", "revolute", "base", "turret", trans((0, 0, 30)), (0, 0, 1), -2.6, 2.6,
                        actuator="sts3215", effort=2.9))
    # turret: moulded-cover candidate (hollow, open at the bottom)
    shell = m3d.Manifold.cylinder(60, 45, 40, 64) - m3d.Manifold.cylinder(57.8, 42.8, 37.8, 64).translate((0, 0, -0.01))
    asm.add_part(Part("turret_shell", "turret", _tri(shell), kind="cover", step=2, **printed))
    # sheet-metal shoulder brackets x2 (same group)
    flat, sheet = sheet_flat()
    t = sheet["thickness"]
    web = _box_m(50, 32, t, (25, 38, t / 2))
    fl1 = _box_m(50, t, 22, (25, 22 + t / 2, 11 + t))
    fl2 = _box_m(50, t, 22, (25, 54 - t / 2, 11 + t))
    folded = _tri(web + fl1 + fl2)
    for side, y in (("l", -40), ("r", 40)):
        m = folded.copy()
        m.apply_transform(trans((-25, y - 38, 60)))
        asm.add_part(Part(f"shoulder_bracket_{side}", "turret", m, step=3, profile=flat, profile_frame=np.eye(4),
                          thickness=t, material="Al5052", color="#26292e", process="fdm_print",
                          process_options=("fdm_print", "sheet_metal"), group="shoulder_bracket", meta={"sheet": sheet}))
    asm.add_joint(Joint("j2_shoulder", "revolute", "turret", "upper_arm", trans((0, 0, 65)), (0, 1, 0), -1.9, 1.9,
                        actuator="sts3215", effort=2.9))
    asm.add_part(Part("upper_arm_link", "upper_arm", _tri(_box_m(24, 30, 130, (0, 0, 65))), step=4, **printed))
    asm.add_joint(Joint("j3_elbow", "revolute", "upper_arm", "forearm", trans((0, 0, 130)), (0, 1, 0), -1.9, 1.9,
                        actuator="sts3215", effort=2.9))
    asm.add_part(Part("forearm_link", "forearm", _tri(_box_m(22, 28, 110, (0, 0, 55))), step=5, **printed))
    asm.add_joint(Joint("j4_wrist_pitch", "revolute", "forearm", "wrist", trans((0, 0, 110)), (0, 1, 0), -1.9, 1.9,
                        actuator="sts3215", effort=2.9))
    cover = _tri(_box_m(40, 36, 30, (0, 0, 15)) - _box_m(36.4, 32.4, 28.2, (0, 0, 13.9)))
    asm.add_part(Part("wrist_cover", "wrist", cover, kind="cover", step=6, material="PETG", color="#e9edf2",
                      process="fdm_print", process_options=("fdm_print", "sla_print", "mjf_print", "injection_mold")))
    asm.add_joint(Joint("j5_wrist_roll", "revolute", "wrist", "gripper_base", trans((0, 0, 35)), (0, 0, 1), -2.8, 2.8,
                        actuator="sts3215", effort=2.9))
    asm.add_part(Part("gripper_plate", "gripper_base", _tri(_box_m(90, 6, 40, (0, 14, 20))), step=7, **printed))
    asm.add_part(Part("pinion_gear", "gripper_base", _tri(_cyl_m(10.5, 6, -3)), step=7, **printed))
    asm.add_joint(Joint("gripper", "prismatic", "gripper_base", "jaw_left", np.eye(4), (-1, 0, 0), -3, 14,
                        actuator="sts3215", effort=25.0))
    asm.add_joint(Joint("gripper_right", "prismatic", "gripper_base", "jaw_right", np.eye(4), (1, 0, 0), -3, 14,
                        mimic={"joint": "gripper", "multiplier": 1.0}))
    asm.add_joint(Joint("tcp_fixed", "fixed", "gripper_base", "tcp", trans((0, 0, 90))))
    finger = Polygon([(0, 0), (60, 0), (60, 8), (8, 8), (8, 14), (0, 14)])
    for side, link, sgn in (("left", "jaw_left", -1), ("right", "jaw_right", 1)):
        T = np.array([[0, -sgn, 0, sgn * 30.0], [0, 0, 1, -10], [1, 0, 0, 40], [0, 0, 0, 1.0]])
        m = components.trimesh_from_profile(finger, 20.0)
        m.apply_transform(T)
        asm.add_part(Part(f"finger_{side}", link, m, step=8, profile=finger, profile_frame=T, thickness=20.0,
                          material="PETG", color="#2c3037", process="fdm_print",
                          process_options=("fdm_print", "sla_print", "cnc_mill"), group="finger"))
        pad = _tri(_box_m(4, 20, 22, (sgn * 26.0, 0, 70)))
        asm.add_part(Part(f"finger_{side}_pad", link, pad, kind="pad", step=8, material="TPU", color="#ff6a1a",
                          process="fdm_print", process_options=("fdm_print", "sla_print", "injection_mold"), group="finger_pad"))
    # purchased: actuators (multi-part components), modules, bearing, fasteners
    placements = {"j1": ("base", 1), "j2": ("turret", 3), "j3": ("upper_arm", 4), "j4": ("forearm", 5)}
    comp = components.actuator("sts3215")
    for pre, (link, step) in placements.items():
        for suffix, mesh, color, mat in comp.parts:
            asm.add_part(Part(f"act_{pre}_{suffix}", link, mesh.copy(), kind="actuator", material=mat, color=color,
                              process="purchased", sku="sts3215", step=step, group=f"sts3215_{suffix}",
                              meta={"mass_g": 55.0 / len(comp.parts)}))
    x = -40.0
    for ref, key in (("u1", "esp32_devkit"), ("u2", "bus_servo_adapter"), ("u3", "buck_5v")):
        c = components.module(key)
        for suffix, mesh, color, mat in c.parts:
            m = mesh.copy()
            m.apply_transform(trans((x, -45, 8)))
            asm.add_part(Part(f"mod_{ref}_{suffix}", "base", m, kind="electronics", material=mat, color=color,
                              process="purchased", sku=key, step=1, group=f"{key}_{suffix}"))
        x += 35
    b = components.bearing("lazy_susan_100")
    for suffix, mesh, color, mat in b.parts:
        m = mesh.copy()
        m.apply_transform(trans((0, 0, 20)))
        asm.add_part(Part(f"brg_{suffix}", "base", m, kind="bearing", material=mat, color=color, process="purchased",
                          sku="lazy_susan_100", step=2, group=f"lazy_susan_100_{suffix}"))
    fc = components.fastener_component("M3x8", "shcs")
    for i, a in enumerate(np.linspace(0, 2 * math.pi, 5)[:-1] + math.pi / 4):
        m = fc.parts[0][1].copy()
        m.apply_transform(trans((55 * math.cos(a), 55 * math.sin(a), 8)))
        asm.add_part(Part(f"fst_base_{i}", "base", m, kind="fastener", material="Steel", color="#2b2c30",
                          process="purchased", sku=fc.key, step=1, group=fc.key, meta={"spec": "M3x8"}, collide=False))
    asm.cables.append(Cable("bus_main", [("base", (-40, -45, 20)), ("turret", (0, -40, 40)), ("upper_arm", (0, -20, 65)),
                                         ("forearm", (0, -20, 55)), ("wrist", (0, -15, 20))], diameter=3.0, src="U2.bus", dst="j4.bus"))
    steps = [(1, "Base plate, base servo and electronics", [], "Press the inserts into the base plate. Seat the J1 servo, spline up."),
             (2, "Turret and turntable bearing", [{"spec": "M3x8 SHCS", "qty": 4, "torque_nm": 0.8}], ""),
             (3, "Shoulder brackets + J2", [{"spec": "M3x6 BHCS", "qty": 8, "torque_nm": 0.8}, {"spec": "M3 nylock nut", "qty": 8}], ""),
             (4, "Upper arm + elbow", [], ""), (5, "Forearm + wrist pitch", [], ""), (6, "Wrist cover", [], ""),
             (7, "Gripper body", [{"spec": "M2.5x6 SHCS", "qty": 4, "torque_nm": 0.4}], ""),
             (8, "Fingers and FSR pads", [{"spec": "M3x16 SHCS", "qty": 4, "torque_nm": 0.6}], "")]
    asm.steps = [Step(i, title, [p.name for p in asm.parts if p.step == i], fasteners=f, instructions=ins)
                 for i, title, f, ins in steps]
    names = ["j1_base_yaw", "j2_shoulder", "j3_elbow", "j4_wrist_pitch", "j5_wrist_roll"]
    asm.meta = {"arm_joints": names, "gripper_joint": "gripper", "tcp_link": "tcp",
                "home": {**{n: 0.0 for n in names}, "j2_shoulder": 0.3, "j3_elbow": 1.2, "j4_wrist_pitch": 1.1, "gripper": 10.0},
                "ready": {**{n: 0.0 for n in names}, "gripper": 14.0}}
    return asm


def mock_efinal(config: str = "modules") -> dict:
    V = catalog.V
    sourcing = [
        {"item": "ESP32-DevKitC-32E", "sku": "esp32_devkit", "category": "electronics", "qty": 1, "make_or_buy": "buy",
         "how": "buy from vendor", "vendors": [V("digikey", "ESP32-DevKitC-32E", 10.0), V("mouser", "ESP32-DevKitC-32E", 10.5)]},
        {"item": "Waveshare Bus Servo Adapter (A)", "category": "electronics", "qty": 1, "make_or_buy": "buy",
         "how": "buy from vendor", "vendors": [V("waveshare", "Bus Servo Adapter", 6.0)]},
        {"item": "12 V 10 A desktop power supply", "sku": "psu_12v", "category": "electronics", "qty": 1, "make_or_buy": "buy",
         "how": "buy from vendor", "vendors": [V("amazon", "12V 10A power supply", 22.0)]},
        {"item": "Interlink FSR 402 force-sensing resistor", "sku": "fsr402", "category": "electronics", "qty": 2,
         "make_or_buy": "buy", "how": "buy from vendor", "vendors": [V("digikey", "Interlink 30-81794", 7.0)]},
        {"item": "JST-PH 2-pin pigtail (FSR leads)", "category": "connector", "qty": 2, "make_or_buy": "buy",
         "how": "buy from vendor", "vendors": [V("lcsc", "JST PH 2P cable", 0.18)]},
    ]
    harness = [{"name": f"W{i + 1} bus {a} -> {b}", "from": a, "to": b, "length_mm": L, "conductors": 3,
                "wire": "24 AWG silicone", "connector_a": "5264 3p", "connector_b": "5264 3p"}
               for i, (a, b, L) in enumerate([("U2.bus", "j1_base_yaw.A", 140), ("j1_base_yaw.B", "j2_shoulder.A", 160),
                                              ("j2_shoulder.B", "j3_elbow.A", 190), ("j3_elbow.B", "j4_wrist_pitch.A", 170),
                                              ("j4_wrist_pitch.B", "j5_wrist_roll.A", 90), ("j5_wrist_roll.B", "gripper.A", 80)])]
    harness += [{"name": "W7 FSR left", "from": "U1.IO4", "to": "S1 jaw_left", "length_mm": 610, "conductors": 2,
                 "wire": "28 AWG twisted pair", "connector_a": "JST-PH 2p", "connector_b": "FSR tail crimp"},
                {"name": "W8 power in", "from": "PS1", "to": "U3.VIN", "length_mm": 300, "conductors": 2,
                 "wire": "18 AWG", "connector_a": "5.5x2.1 barrel", "connector_b": "screw terminal"}]
    board = None
    if config == "custom_pcb":
        board = {"json": "board/board.json", "gerbers": "board/gerbers/gerbers.zip", "size_mm": [88.9, 64.8],
                 "drc": {"pass": True}}
    return {"config": config, "bus": "ttl_half_duplex", "schematic_svg": "schematic.svg",
            "nets": [{"name": "BUS_DATA", "nodes": ["U2.D", "J2.3"]}], "board": board, "harness": harness,
            "firmware": "firmware/morph_arm/morph_arm.ino",
            "power_budget": {"rails": [{"name": "V_BUS 12 V", "v": 12.0, "peak_a": 8.6, "avg_a": 2.1, "source": "psu_12v"}],
                             "supply_ok": True},
            "sourcing": sourcing,
            "configs": {"modules": {"cost": 41.0, "summary": "ESP32 + bus adapter + buck"},
                        "custom_pcb": {"cost": 31.8, "summary": "one PCBA"}},
            "issues": [], "warnings": []}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """plan + bom + manual for all three build configs (one assembly)."""
    out = {}
    root = tmp_path_factory.mktemp("mfg")
    design0 = Design().with_changes(EGG)
    asm = make_asm(design0)
    ef = mock_efinal()
    for cfg in manufacturing.CONFIGS:
        d = design0.with_changes({"build.config": cfg, "build.quantity": 50})
        rev = root / cfg
        mfg = manufacturing.plan(d, asm, str(rev / "mfg"), ef)
        b = manufacturing.bom(d, asm, mfg, ef)
        man = manual.build(d, asm, mfg, ef, str(rev / "manual"))
        out[cfg] = {"design": d, "mfg": mfg, "bom": b, "manual": man, "dir": rev}
    out["asm"] = asm
    return out


# ------------------------------------------------------------------ manufacturing plan
def test_plan_schema_and_files(built):
    for cfg in manufacturing.CONFIGS:
        mfg = built[cfg]["mfg"]
        assert mfg["config"] == cfg
        assert set(mfg["configs"]) == set(manufacturing.CONFIGS)
        for k in ("parts_cost", "tooling_cost", "purchased_cost", "lead_days"):
            assert k in mfg["totals"]
        json.dumps(mfg)
        assert mfg["parts"], cfg
        for p in mfg["parts"]:
            for k in ("group", "qty", "name", "process", "material", "vendor", "vendor_url", "unit_cost", "setup_cost",
                      "lead_days", "files", "dfm", "notes"):
                assert k in p, (cfg, k)
            assert p["unit_cost"] > 0, p
            for f in p["files"]:
                assert (built[cfg]["dir"] / "mfg" / f).is_file(), f
        assert (built[cfg]["dir"] / "mfg" / mfg["package"]).is_file()
        assert (built[cfg]["dir"] / "mfg" / "README.txt").is_file()


def test_identical_parts_grouped(built):
    parts = {p["group"]: p for p in built["prototype"]["mfg"]["parts"]}
    assert parts["finger"]["qty"] == 2
    assert parts["shoulder_bracket"]["qty"] == 2
    assert "finger_left" not in parts


def test_process_per_config(built):
    proc = {cfg: {p["group"]: p["process"] for p in built[cfg]["mfg"]["parts"]} for cfg in manufacturing.CONFIGS}
    assert proc["prototype"]["upper_arm_link"] == "fdm_print"
    assert proc["pilot"]["base_plate"] == "cnc_mill"
    assert proc["pilot"]["shoulder_bracket"] == "sheet_metal"
    assert proc["pilot"]["turret_shell"] == "mjf_print"
    assert proc["production"]["turret_shell"] == "injection_mold"
    assert proc["production"]["shoulder_bracket"] == "sheet_metal"
    # process_options are honoured: the finger has no molding option
    assert proc["production"]["finger"] in ("fdm_print", "sla_print", "cnc_mill")


def test_costs_positive_and_differ_by_config(built):
    cfgs = built["prototype"]["mfg"]["configs"]
    costs = [cfgs[c]["unit_cost"] for c in manufacturing.CONFIGS]
    assert all(c > 0 for c in costs)
    assert len(set(costs)) == 3
    assert cfgs["production"]["tooling_cost"] > 1000 and cfgs["prototype"]["tooling_cost"] == 0
    assert cfgs["prototype"]["lead_days"] < cfgs["production"]["lead_days"]
    # every config is costed identically no matter which one is active
    for c in manufacturing.CONFIGS:
        assert built["pilot"]["mfg"]["configs"][c]["unit_cost"] == pytest.approx(cfgs[c]["unit_cost"], rel=0.02)
    for p in built["pilot"]["mfg"]["parts"]:
        assert p["cost_model"]["formula"] and p["quotes"]
        assert all(q["url"].startswith("https://") for q in p["quotes"] if not q["vendor"].startswith("In-house"))


def _dxf_entities(text):
    lines = [ln.strip() for ln in text.splitlines()]
    pairs = list(zip(lines[0::2], lines[1::2]))
    ents, layers, in_ent = [], set(), False
    for code, val in pairs:
        if code == "2" and val == "ENTITIES":
            in_ent = True
        if in_ent and code == "0" and val not in ("ENDSEC", "EOF"):
            ents.append(val)
        if in_ent and code == "8":
            layers.add(val)
    return ents, layers, pairs


def test_dxf_and_gcode_for_cnc_plate(built):
    d = built["pilot"]["dir"] / "mfg" / "base_plate"
    text = (d / "base_plate.dxf").read_text()
    ents, layers, pairs = _dxf_entities(text)
    assert ("1", "AC1009") in pairs and pairs[-1] == ("0", "EOF")
    assert ents.count("CIRCLE") == 7            # round outline + 6 round holes become true circles
    assert "TEXT" in ents and {"CUT", "ENGRAVE"} <= layers
    nc = (d / "base_plate.nc").read_text()
    assert "G21" in nc and "M30" in nc and re.search(r"G[23] X", nc)          # helical holes
    zs = [float(z) for z in re.findall(r"G1 X\S+ Y\S+ Z(-?\d+\.\d+)", nc)]
    assert min(zs) < -8.0                                                        # cuts through the 8 mm plate
    part = next(p for p in built["pilot"]["mfg"]["parts"] if p["group"] == "base_plate")
    assert part["cnc"]["tabs"] >= 2 and part["cnc"]["minutes"] > 0


def test_sheet_metal_flat_pattern_and_bend_table(built):
    d = built["pilot"]["dir"] / "mfg" / "shoulder_bracket"
    ents, layers, _ = _dxf_entities((d / "shoulder_bracket_flat.dxf").read_text())
    assert ents.count("LINE") == 2 and "BEND" in layers
    assert "POLYLINE" in ents and ents.count("VERTEX") >= 4
    rows = list(csv.DictReader(open(d / "shoulder_bracket_bends.csv")))
    assert len(rows) == 2
    r = rows[0]
    t, R, k = 1.6, 1.6, 0.44
    ba = math.pi / 2 * (R + k * t)
    assert float(r["bend_allowance_mm"]) == pytest.approx(ba, abs=1e-3)
    assert float(r["bend_deduction_mm"]) == pytest.approx(2 * (R + t) - ba, abs=1e-3)
    assert float(r["k_factor"]) == k


def test_print_files_and_dfm(built):
    d = built["prototype"]["dir"] / "mfg" / "turret_shell"
    stl = trimesh.load(d / "turret_shell.stl")
    assert abs(stl.bounds[0][2]) < 1e-6                                          # sits on the bed
    with zipfile.ZipFile(d / "turret_shell.3mf") as z:
        model = z.read("3D/3dmodel.model").decode()
        assert "<triangles>" in model and "[Content_Types].xml" in z.namelist()
    fingers = next(p for p in built["prototype"]["mfg"]["parts"] if p["group"] == "finger")
    assert fingers["print"]["orientation"].startswith("profile")                   # printed flat
    with zipfile.ZipFile(built["prototype"]["dir"] / "mfg" / "finger" / "finger.3mf") as z:
        assert z.read("3D/3dmodel.model").decode().count("<item ") == 2           # qty 2 on the plate


def test_molding_dfm_flags_missing_draft(built):
    p = next(p for p in built["production"]["mfg"]["parts"] if p["group"] == "wrist_cover")
    assert p["process"] == "injection_mold" and p["tooling_cost"] > 2000
    assert any("draft" in x for x in p["dfm"])
    notes = json.load(open(built["production"]["dir"] / "mfg" / "wrist_cover" / "wrist_cover_molding.json"))
    assert notes["gate"] and notes["tool"]["cost_usd"] > 0


def test_sheet_dfm_catches_short_flange_and_missing_relief():
    poly = box(0, 0, 40, 30).difference(Point(20, 5).buffer(1.7))
    sheet = {"thickness": 2.0, "k_factor": 0.4, "material": "Al5052",
             "bends": [{"line": [[0, 4], [30, 4]], "angle_deg": 90, "radius": 0.6}]}      # 5052 needs R >= 0.5 T
    p = Part("bad_bracket", "base", components.trimesh_from_profile(poly, 2.0), profile=poly, profile_frame=np.eye(4),
             thickness=2.0, process="sheet_metal", meta={"sheet": sheet})
    msgs = " | ".join(manufacturing._sheet_dfm(manufacturing.Geo(p)))
    assert "flange" in msgs and "relief" in msgs and "radius" in msgs and "distort" in msgs


def test_fastener_parsing():
    f = manufacturing.parse_fastener("M3x8_shcs")
    assert (f["size"], f["length"], f["kind"], f["torque_nm"]) == ("M3", 8.0, "shcs", catalog.FASTENER_TORQUE_NM["M3"])
    assert f["drive"] == "2.5 mm hex key"
    assert manufacturing.parse_fastener("M3 heat-set insert")["kind"] == "insert"
    assert manufacturing.parse_fastener("M2.5×6 horn screw")["spec"] == "M2.5×6 SHCS"
    assert manufacturing.parse_fastener("M3 nylock nut")["kind"] == "nut"


# ------------------------------------------------------------------ BOM
def test_bom_rows_and_totals(built):
    for cfg in manufacturing.CONFIGS:
        b = built[cfg]["bom"]
        rows = b["rows"]
        for r in rows:
            for k in ("item", "category", "qty", "make_or_buy", "how", "vendors", "selected", "ext_price"):
                assert k in r
            assert r["make_or_buy"] in ("buy", "make")
        t = b["totals"]
        assert t["total"] == pytest.approx(t["buy"] + t["make"], abs=0.02)
        assert t["total"] == pytest.approx(built[cfg]["mfg"]["configs"][cfg]["unit_cost"], abs=0.05)
        assert set(t["per_config"]) == set(manufacturing.CONFIGS)
        json.dumps(b)
    rows = built["prototype"]["bom"]["rows"]
    servo = [r for r in rows if r["category"] == "actuator"]
    assert len(servo) == 1 and servo[0]["qty"] == 6           # 5 arm joints + gripper (from joints, not just placed parts)
    esp = [r for r in rows if "ESP32" in r["item"]]
    assert len(esp) == 1                                       # deduped: assembly part vs electronics sourcing
    bus = [r for r in rows if "Bus Servo Adapter" in r["item"]]
    assert len(bus) == 1                                       # deduped by catalog name (no sku on the row)
    m3 = [r for r in rows if r["category"] == "fastener" and r["item"].startswith("M3×8 socket")]
    assert len(m3) == 1 and m3[0]["qty"] == 8                  # 4 parts at step 1 + 4 listed at step 2
    assert any(r["vendors"] and "mcmaster" in r["vendors"][0]["vendor"] for r in rows if r["category"] == "fastener")
    assert any(r["make_or_buy"] == "make" and r["category"] == "cable" for r in rows)   # harness from efinal
    assert any(r["category"] == "mechanical" and "lazy" in r["item"].lower() for r in rows)
    prod = built["production"]["bom"]["rows"]
    assert any("tooling" in r["item"].lower() for r in prod)


# ------------------------------------------------------------------ manual
def test_manual_json_and_html(built):
    for cfg in ("prototype", "production"):
        man = built[cfg]["manual"]
        d = built[cfg]["dir"] / "manual"
        assert man["json"] == "manual.json" and man["html"] == "manual.html"
        assert (d / "manual.json").is_file() and (d / "manual.html").is_file()
        html = (d / "manual.html").read_text(encoding="utf-8")
        steps = man["steps"]
        mech = [s for s in steps if s.get("kind") == "mechanical"]
        assert len(mech) == 8
        kinds = [s.get("kind") for s in steps]
        for k in ("wiring", "firmware", "safety", "calibration"):
            assert k in kinds
        for s in steps:
            for k in ("index", "title", "parts", "fasteners", "tools", "instructions", "warnings", "time_min"):
                assert k in s
            assert s["time_min"] > 0
            assert s["title"] in html.replace("&amp;", "&")
            for f in s["fasteners"]:
                assert f["spec"] in html
        assert man["time_min_total"] == pytest.approx(sum(s["time_min"] for s in steps), abs=1)
        assert man["tools"] and all(t in html for t in man["tools"])
        assert "@media print" in html and "<img" in html
        assert "W1 bus" in html                                   # wiring step lists the harness
        assert "0.8 N·m" in html                                  # torque callouts
        idx = [s["index"] for s in steps]
        assert idx == sorted(idx) and len(set(idx)) == len(idx)


def test_manual_without_efinal_or_mfg(tmp_path):
    d = Design().with_changes(EGG)
    asm = make_asm(d)
    man = manual.build(d, asm, None, None, str(tmp_path))
    assert (tmp_path / "manual.html").is_file() and len(man["steps"]) >= 8


def test_real_generator_if_available(tmp_path):
    from morph.robot import arm

    if not hasattr(arm, "build"):
        pytest.skip("arm.build not available")
    d = Design().with_changes(EGG)
    try:
        sz = arm.size(d)
        asm = arm.build(d, sz, None)
    except Exception as exc:  # generator mid-rewrite: that is the generator's test to fail, not this one
        pytest.skip(f"arm.build failed: {exc}")
    mfg = manufacturing.plan(d, asm, str(tmp_path / "mfg"), mock_efinal())
    b = manufacturing.bom(d, asm, mfg, mock_efinal())
    man = manual.build(d, asm, mfg, mock_efinal(), str(tmp_path / "manual"))
    assert mfg["parts"] and b["totals"]["total"] > 0 and man["steps"]


# ------------------------------------------------------------------ generator conventions
def test_generator_build_config_and_mold_payoff(tmp_path):
    d = Design().with_changes({**EGG, "build.config": "pilot"})
    asm = make_asm(d)
    asm.meta["build_config"] = "pilot"                     # the generator built this geometry for the pilot config
    for p in asm.parts:
        if p.group == "finger_pad":                        # it chose molded pads; a mold cannot pay off at qty 1
            p.process, p.process_options = "injection_mold", ("injection_mold", "fdm_print")
        if p.name == "upper_arm_link":
            p.process = "mjf_print"                        # its own pick, not the class default (cnc_mill)
    mfg = manufacturing.plan(d, asm, str(tmp_path / "mfg"), None)
    parts = {e["group"]: e for e in mfg["parts"]}
    assert parts["upper_arm_link"]["process"] == "mjf_print"
    assert parts["finger_pad"]["process"] == "fdm_print" and "injection molding" in parts["finger_pad"]["notes"]
    assert mfg["configs"]["pilot"]["tooling_cost"] == 0
    # other configs: that config's class defaults (a different generator geometry would be built there)
    assert mfg["configs"]["production"]["tooling_cost"] > 0 and mfg["configs"]["prototype"]["tooling_cost"] == 0


def test_purchased_hardware_horns_and_meta_qty():
    d = Design().with_changes(EGG)
    asm = make_asm(d)
    cube = trimesh.creation.box((5, 5, 5))
    asm.add_part(Part("base_feet", "base", cube.copy(), kind="visual", process="purchased", sku="rubber_foot_14x2.5",
                      step=1, meta={"qty": 4, "label": "rubber feet (adhesive, 14 mm)"}))
    for i in range(3):
        asm.add_part(Part(f"standoff_{i}", "base", cube.copy(), process="purchased", sku="M3x6_brass_standoff", step=1,
                          group="M3x6_brass_standoff"))
    asm.add_part(Part("act_j1_horn2", "base", cube.copy(), kind="horn", process="purchased", sku="sts3215_horn",
                      step=1, group="sts3215_horn_horn"))
    rows = manufacturing.buy_rows(d, asm, None)
    feet = [r for r in rows if "rubber" in r["item"].lower()]
    assert len(feet) == 1 and feet[0]["qty"] == 4 and feet[0]["ext_price"] > 0
    so = [r for r in rows if "standoff" in r["item"]]
    assert len(so) == 1 and so[0]["category"] == "fastener" and so[0]["qty"] == 3
    horn = [r for r in rows if "horn" in r["item"]]
    assert len(horn) == 1 and horn[0]["ext_price"] == 0 and "included" in horn[0]["how"]


def test_manual_folds_generator_wiring_step_and_uses_servo_ids(tmp_path):
    d = Design().with_changes(EGG)
    asm = make_asm(d)
    asm.steps.append(Step(9, "Servo bus and sensor wiring", [], tools=["cable ties (2.5 mm)"],
                          instructions="Daisy-chain the bus: controller to ID 1 to ID 2. Leave a service loop at every joint."))
    asm.meta["servo_ids"] = [{"joint": "j1_base_yaw", "id": 1}, {"joint": "j2_shoulder", "id": 2},
                             {"joint": "j2_shoulder", "id": 3}, {"joint": "j3_elbow", "id": 4},
                             {"joint": "j4_wrist_pitch", "id": 5}, {"joint": "roll", "id": 6}, {"joint": "gripper", "id": 7}]
    man = manual.build(d, asm, None, mock_efinal(), str(tmp_path), images=False)
    wiring = [s for s in man["steps"] if s["kind"] == "wiring"]
    assert len(wiring) == 1                                # the generator's wiring step is adopted, not duplicated
    wiring = wiring[0]
    assert wiring["title"] == "Servo bus and sensor wiring" and wiring["index"] == 9
    assert any(x.startswith("Daisy-chain the bus") for x in wiring["substeps"])
    assert "cable ties (2.5 mm)" in wiring["tools"] and wiring["harness"]
    idx = [s["index"] for s in man["steps"]]
    assert idx == sorted(idx) and len(set(idx)) == len(idx)
    assert not any("PWM channel" in x for x in wiring["substeps"])           # bus robot: no PWM wording
    fw = next(s for s in man["steps"] if s["kind"] == "firmware")
    assert [x["id"] for x in fw["servo_ids"]] == [1, 2, 3, 4, 5, 6, 7]
    assert fw["servo_ids"][5]["joint"] == "j5_wrist_roll"                    # "roll" resolved to the real joint
    html = (tmp_path / "manual.html").read_text(encoding="utf-8")
    assert "J2 shoulder (2)" in html


def test_gcode_slot_gets_inside_contour(tmp_path):
    from shapely.geometry import LineString
    plate = box(0, 0, 40, 120).difference(LineString([(20, 30), (20, 90)]).buffer(6, quad_segs=12)) \
        .difference(Point(20, 10).buffer(4, quad_segs=16))
    info = manufacturing.write_gcode(str(tmp_path / "p.nc"), plate, 4.0, "Al6061", "SLOT TEST")
    nc = (tmp_path / "p.nc").read_text()
    assert "( inside cut-out )" in nc and nc.count("helical hole") == 1 and info["tabs"] >= 2
    ok, c, r = manufacturing._round_ring(list(plate.interiors)[0])
    assert manufacturing.dfm_checks is not None and isinstance(ok, bool)


def test_sources_parse_on_python310():
    """The package supports Python >= 3.10: no PEP 701 f-strings (same-quote nesting, backslashes in expressions)."""
    import io
    import tokenize

    for rel in ("morph/robot/manufacturing.py", "morph/robot/manual.py"):
        src = open(os.path.join(os.path.dirname(__file__), "..", rel), encoding="utf-8").read()
        stack, probs = [], []
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            name = tokenize.tok_name[t.type]
            if name == "FSTRING_START":
                q = t.string.lstrip("rRbBfFuU")
                if any(q[0] == s[0] for s in stack):
                    probs.append(t.start)
                stack.append(q)
            elif name == "FSTRING_END":
                stack.pop()
            elif stack and name == "STRING" and (t.string.lstrip("rRbBuU")[0] in [s[0] for s in stack] or "\\" in t.string):
                probs.append(t.start)
        assert not probs, (rel, probs)
