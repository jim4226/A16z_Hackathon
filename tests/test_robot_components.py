"""Tests for the purchased-part geometry library (morph/robot/components.py)."""

import math
import time

import numpy as np
import pytest
import trimesh
from shapely.geometry import Point, Polygon, box

from morph.robot import catalog
from morph.robot import components as C

try:   # runtime catalog extensions (imported robots) must build too
    from morph.robot import catalog_extra  # noqa: F401
except Exception:  # pragma: no cover
    pass

ACT_FRAMES = ("output", "horn_seat", "mount_bottom", "idler", "connector", "body_center", "case_top")


def _within(value, target, tol=0.05):
    return abs(value - target) <= tol * target + 1e-6


def _bounds(meshes):
    return trimesh.util.concatenate(meshes).bounds


def _check_parts(comp):
    assert comp.parts, comp.key
    for suffix, mesh, color, material in comp.parts:
        assert len(mesh.faces) > 0, (comp.key, suffix)
        assert mesh.is_watertight, (comp.key, suffix)
        assert mesh.is_winding_consistent, (comp.key, suffix)
        assert mesh.volume > 0, (comp.key, suffix)
        assert color.startswith("#") and len(color) == 7, (comp.key, suffix)
        assert all(c.isalnum() or c == "_" for c in suffix), suffix
        from morph.robot.assembly import MATERIAL_PBR
        assert material in MATERIAL_PBR, (comp.key, suffix, material)


# ------------------------------------------------------------------ helpers
def test_rounded_box_dims_and_watertight():
    m = C.rounded_box(40, 20, 10, r=3)
    assert m.is_watertight
    assert np.allclose(m.extents, [40, 20, 10], atol=1e-6)
    assert np.allclose(m.bounds.mean(axis=0), 0, atol=1e-6)
    f = C.rounded_box(40, 20, 10, r=3, fillet=2, center=(5, 0, 5))
    assert f.is_watertight and np.allclose(f.bounds[0], [-15, -10, 0], atol=1e-6)
    assert f.volume < m.volume < 40 * 20 * 10


def test_cylinder_tube_hex_helpers():
    c = C.chamfered_cylinder(5, 10, chamfer=0.5, z0=2)
    assert c.is_watertight and np.allclose(c.bounds[:, 2], [2, 12])
    t = C.tube(6, 4, 3, chamfer=0.2)
    assert t.is_watertight and abs(t.volume - math.pi * (36 - 16) * 3) / (math.pi * 60) < 0.05
    h = C.hex_prism(5.5, 2.4, chamfer=0.4, hole=3.0)
    assert h.is_watertight
    assert _within(h.extents[1], 5.5, 0.01)          # across flats along y
    assert _within(h.extents[0], 5.5 / math.cos(math.pi / 6), 0.01)


def test_trimesh_from_profile_chamfered_with_holes():
    poly = box(0, 0, 30, 12).difference(Point(8, 6).buffer(1.7, quad_segs=12)).difference(Point(22, 6).buffer(1.7, quad_segs=12))
    m = C.trimesh_from_profile(poly, 6.0, chamfer=0.4)
    assert m.is_watertight
    assert np.allclose(m.bounds, [[0, 0, 0], [30, 12, 6]], atol=1e-6)
    assert 0.9 * poly.area * 6 < m.volume < poly.area * 6
    plain = C.trimesh_from_profile(poly, 6.0, chamfer=0.0)
    assert abs(plain.volume - poly.area * 6) < 1e-3 * poly.area * 6
    rounded = C.trimesh_from_profile(poly, 6.0, fillet=0.8)
    assert rounded.is_watertight and rounded.volume < plain.volume


def test_trimesh_from_profile_on_finger_profiles():
    from morph.design import build_profile
    from morph.spec import Design

    d = Design()
    d.finger.sensor = "fsr402"
    geom = build_profile(d)
    for name, poly in geom.part_polys().items():
        t0 = time.time()
        m = C.trimesh_from_profile(poly, d.finger.width_mm)
        assert time.time() - t0 < 1.0
        assert m.is_watertight, name
        assert np.allclose(m.bounds[:, 2], [0, d.finger.width_mm])
        assert np.allclose(m.bounds[:, :2], np.asarray(poly.bounds).reshape(2, 2), atol=0.05)


def test_gear_offset_stack_text():
    from morph.mechanism import pinion_profile

    g = C.gear_mesh(pinion_profile(14), 6.0, bore=5.0)
    assert g.is_watertight and np.allclose(g.bounds[:, 2], [0, 6])
    s = C.offset_stack(Polygon([(0, 0), (20, 0), (20, 10), (0, 10)]), [(0, 1.0), (1, 0.0), (5, 0.0), (6, 1.0)])
    assert s.is_watertight and np.allclose(s.extents, [20, 10, 6], atol=1e-6)
    t = C.text_mesh("MORPH 42", 5.0, relief=0.2)
    assert len(t.faces) > 0 and t.extents[1] <= 5.0 * 1.2


# ------------------------------------------------------------------ actuators
@pytest.mark.parametrize("key", sorted(catalog.ACTUATORS))
def test_actuator(key):
    comp = C.actuator(key)
    spec = catalog.ACTUATORS[key]
    d = spec["dims"]
    assert comp.kind == "actuator" and comp.spec["key"] == key
    _check_parts(comp)
    for f in ACT_FRAMES:
        assert f in comp.frames and comp.frames[f].shape == (4, 4), f
    assert np.allclose(comp.frames["output"], np.eye(4))
    suffixes = [s for s, *_ in comp.parts]
    assert "case" in suffixes and "horn" in suffixes
    # horn face flush with the output frame, body below it
    horn = next(m for s, m, *_ in comp.parts if s == "horn")
    assert abs(horn.bounds[1][2]) < 0.05
    assert comp.mesh().bounds[1][2] < 0.6
    # case dims within 5 % of the catalog (l along x, w along y, h between case top / bottom)
    case = next(m for s, m, *_ in comp.parts if s == "case")
    ext = case.extents
    assert _within(ext[0], d["l"]), (ext, d)
    assert _within(ext[1], d["w"]), (ext, d)
    assert _within(comp.dims["case_top_z"] - comp.dims["case_bottom_z"], d["h"])
    assert ext[2] >= 0.95 * d["h"]
    # body centre sits shaft_off towards -x (stub / SO-101 STL convention)
    assert abs(comp.frames["body_center"][0, 3] + d["shaft_off"]) < 1e-6
    assert abs((case.bounds[0][0] + case.bounds[1][0]) / 2 + d["shaft_off"]) < 0.6
    # mount_bottom on the bottom face, z pointing out of it
    mb = comp.frames["mount_bottom"]
    assert abs(mb[2, 3] - comp.dims["case_bottom_z"]) < 1e-6
    assert np.allclose(mb[:3, 2], [0, 0, -1])
    assert np.allclose(comp.frames["idler"][:3, 2], [0, 0, -1])
    assert comp.dims["overall_h"] > d["h"]


@pytest.mark.parametrize("key", ["sts3215", "xl430_w250", "mg996r"])
def test_actuator_without_horn_and_copies(key):
    bare = C.actuator(key, horn=False)
    assert not any(s.startswith("horn") for s, *_ in bare.parts)
    assert np.allclose(bare.frames["output"], np.eye(4))
    a = C.actuator(key)
    a.parts[0][1].apply_translation((100, 0, 0))
    a.frames["output"][0, 3] = 5.0
    b = C.actuator(key)                       # cached builds hand out independent copies
    assert b.parts[0][1].bounds[1][0] < 60
    assert np.allclose(b.frames["output"], np.eye(4))


def test_hobby_servo_details():
    for key in catalog.FAMILY_ACTUATORS["hobby_pwm"]:
        comp = C.actuator(key)
        suffixes = {s for s, *_ in comp.parts}
        assert {"ears", "wire_brown", "wire_red", "wire_orange", "spline"} <= suffixes
        assert "mount_ears" in comp.frames
        ears = next(m for s, m, *_ in comp.parts if s == "ears")
        assert ears.extents[0] > catalog.ACTUATORS[key]["dims"]["ear_l"]
        assert comp.dims["holes"]["ears"]


@pytest.mark.parametrize("key", sorted(catalog.ACTUATORS))
def test_horn(key):
    h = C.horn(key)
    _check_parts(h)
    t = h.dims["t"]
    assert np.allclose(h.frames["face"], np.eye(4) @ np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, t], [0, 0, 0, 1.0]]))
    b = h.mesh().bounds
    assert abs(b[1][2] - t) < 0.05 and abs(b[0][2]) < 0.05
    assert _within(b[1][0] - b[0][0], catalog.ACTUATORS[key]["dims"]["horn_d"])
    # the actuator's horn_seat frame is where this horn goes
    a = C.actuator(key)
    assert abs(a.frames["horn_seat"][2, 3] + t) < 1e-6


# ------------------------------------------------------------------ bearings / fasteners
@pytest.mark.parametrize("key", sorted(catalog.BEARINGS))
def test_bearing(key):
    b = C.bearing(key)
    spec = catalog.BEARINGS[key]
    _check_parts(b)
    ext = b.mesh().extents
    assert _within(ext[0], spec["D"]) and _within(ext[1], spec["D"])
    assert _within(ext[2], spec["B"])
    assert "center" in b.frames and np.allclose(b.frames["center"], np.eye(4))
    # the bore is clear: nothing inside d / 2
    v = b.mesh().vertices
    assert _within(float(np.hypot(v[:, 0], v[:, 1]).min()), spec["d"] / 2)


@pytest.mark.parametrize("kind", C.FASTENER_KINDS)
@pytest.mark.parametrize("size", sorted(catalog.FASTENER_SIZES))
def test_fastener(kind, size):
    m = C.fastener(f"{size}x10", kind)
    assert m.is_watertight and m.volume > 0
    d = float(size[1:])
    if kind in ("shcs", "bhcs"):
        assert m.bounds[1][2] > 0.5                       # head above the seat
        assert abs(m.bounds[0][2] + 10.0) < 0.05          # shank length below
        assert _within(m.extents[0], max(catalog.FASTENER_SIZES[size][0], d), 0.15)
    elif kind == "fhcs":
        assert abs(m.bounds[1][2]) < 0.05 and abs(m.bounds[0][2] + 10.0) < 0.05
    else:
        assert m.bounds[1][2] <= 0.05 and m.bounds[0][2] < 0     # seats at z = 0, body along -z
    fc = C.fastener_component(f"{size}x10", kind)
    assert fc.kind == "fastener" and "seat" in fc.frames and fc.parts[0][2].startswith("#")


def test_fastener_aliases():
    assert len(C.fastener("M3x8", "socket_head").faces) == len(C.fastener("M3x8", "shcs").faces)
    with pytest.raises(ValueError):
        C.fastener("M3x8", "rivet")


# ------------------------------------------------------------------ modules
@pytest.mark.parametrize("key", sorted(catalog.MODULES))
def test_module(key):
    comp = C.module(key)
    spec = catalog.MODULES[key]
    d = spec["dims"]
    _check_parts(comp)
    assert comp.spec["key"] == key
    assert np.allclose(comp.frames["mount"], np.eye(4))
    assert "connector_a" in comp.frames
    for name, T in comp.frames.items():
        assert T.shape == (4, 4) and np.allclose(T[:3, :3] @ T[:3, :3].T, np.eye(3), atol=1e-9), name
    body = [m for s, m, *_ in comp.parts if not C.is_accessory(s)]
    b = _bounds(body)
    ext = b[1] - b[0]
    if key == "fsr402":
        full = comp.mesh().extents
        assert _within(ext[1], d["w"]) and _within(full[0], d["l"] + d["tail"])
        sub = next(m for s, m, *_ in comp.parts if s == "substrate")
        assert sub.extents[2] <= d["h"]
    else:
        assert _within(ext[0], d["l"]), (ext, d)
        assert _within(ext[1], d["w"]), (ext, d)
        assert _within(ext[2], d["h"]), (ext, d)
        assert np.allclose((b[0][:2] + b[1][:2]) / 2, 0, atol=0.05 * max(d["l"], d["w"]))
    assert b[0][2] > -1.0                                # sits on its mount face
    if "pcb" in {s for s, *_ in comp.parts}:
        assert next(mat for s, _, _, mat in comp.parts if s == "pcb") == "FR4"


def test_module_recognizable_details():
    nano = {s for s, *_ in C.module("arduino_nano").parts}
    assert {"pcb", "headers", "pins", "chips", "metal", "leads"} <= nano
    esp = {s for s, *_ in C.module("esp32_devkit").parts}
    assert {"shield", "module_pcb", "pads", "headers"} <= esp
    pca = C.module("pca9685_board")
    hdr = next(m for s, m, *_ in pca.parts if s == "headers")
    assert len(hdr.split(only_watertight=False)) >= 6              # 4 servo blocks + 2 end headers
    assert {"ecap", "terminal"} <= {s for s, *_ in pca.parts}
    assert {"inductor", "passives"} <= {s for s, *_ in C.module("buck_5v").parts}
    assert {"toroid", "winding", "heatsink", "trimpot"} <= {s for s, *_ in C.module("buck_servo").parts}
    assert {"head", "box"} <= {s for s, *_ in C.module("estop").parts}
    cam = C.module("wrist_camera")
    assert "optical" in cam.frames and {"glass", "lens_holder"} <= {s for s, *_ in cam.parts}
    assert "sense" in C.module("fsr402").frames


def test_unknown_catalog_entries_fall_back():
    """Entries other modules register at runtime still get geometry (generic role styling)."""
    catalog.MODULES.setdefault("test_widget_board", dict(name="Test widget board", role="controller",
                                                         dims=dict(l=40.0, w=30.0, h=8.0), vendors=[]))
    catalog.ACTUATORS.setdefault("test_bus_servo", dict(
        name="Test bus servo", family="smart_bus", interface="ttl_half_duplex", torque_nm=1.0, mass_g=40.0,
        dims=dict(l=40.0, w=22.0, h=30.0, shaft_off=10.0, ear_l=0.0, ear_w=0.0, ear_t=0.0, ear_z=0.0, horn_d=20.0,
                  spline_h=3.0), vendors=[]))
    try:
        m = C.module("test_widget_board")
        _check_parts(m)
        a = C.actuator("test_bus_servo")
        _check_parts(a)
        case = next(mm for s, mm, *_ in a.parts if s == "case")
        assert _within(case.extents[0], 40.0) and _within(case.extents[1], 22.0)
    finally:
        catalog.MODULES.pop("test_widget_board", None)
        catalog.ACTUATORS.pop("test_bus_servo", None)


# ------------------------------------------------------------------ showcase + runtime
def test_showcase_assembly_exports(tmp_path):
    asm = C.showcase()
    assert asm.validate() == []
    keys = {p.sku for p in asm.parts}
    assert set(catalog.ACTUATORS) <= keys and set(catalog.MODULES) <= keys and set(catalog.BEARINGS) <= keys
    path = asm.export_glb(str(tmp_path / "showcase.glb"))
    scene = trimesh.load(path)
    assert len(scene.geometry) == len(asm.parts)


def test_cold_build_is_fast():
    for f in (C._actuator_cached, C._module_cached, C._horn_cached, C._bearing_cached, C._fastener_cached):
        f.cache_clear()
    t0 = time.time()
    for k in catalog.ACTUATORS:
        C.actuator(k)
    for k in catalog.MODULES:
        C.module(k)
    for k in catalog.BEARINGS:
        C.bearing(k)
    for kind in C.FASTENER_KINDS:
        C.fastener("M3x10", kind)
    assert time.time() - t0 < 10.0
    t1 = time.time()
    for _ in range(20):
        C.actuator("sts3215")
    assert time.time() - t1 < 1.0      # cached copies are cheap
