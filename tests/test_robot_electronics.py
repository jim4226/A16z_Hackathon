"""Whole-robot electronics: plan/finalize for every actuator family x configuration (+ extras),
schematic SVG validity, custom-PCB DRC + Gerbers + JLCPCB files, harness, firmware syntax."""

import csv
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

from morph.cli import EGG_ROBOT
from morph.robot import arm, catalog
from morph.robot import electronics2 as e2
from morph.robot import pcb, schematic
from morph.spec import Design

FAMILIES = ["hobby_pwm", "smart_bus", "dynamixel"]
CONFIGS = ["modules", "custom_pcb"]
STUBS = os.path.join(os.path.dirname(__file__), "fw_stubs")


def _design(fam, cfg, extras=""):
    return Design().with_changes({**EGG_ROBOT, "arm.actuator_family": fam, "electronics.config": cfg,
                                  "electronics.extras": extras})


@pytest.fixture(scope="session", autouse=True)
def _pcb_cache(tmp_path_factory):
    os.environ.setdefault("MORPH_PCB_CACHE", str(tmp_path_factory.mktemp("pcb_cache")))


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """finalize() outputs for 3 families x 2 configs x extras, computed once per session."""
    out = {}

    def get(fam, cfg, extras):
        key = (fam, cfg, extras)
        if key not in out:
            d = _design(fam, cfg, extras)
            s = arm.size(d)
            p = e2.plan(d, s)
            a = arm.build(d, s, p)
            od = str(tmp_path_factory.mktemp(f"{fam}_{cfg}_{extras.replace(',', '_') or 'none'}") / "r004" / "electronics")
            out[key] = (d, s, p, a, od, e2.finalize(d, s, p, a, od))
        return out[key]
    return get


# ------------------------------------------------------------------ plan

@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("cfg", CONFIGS)
@pytest.mark.parametrize("extras", ["", "estop,wrist_camera", "imu,wrist_camera,estop,wifi"])
def test_plan_contract(fam, cfg, extras):
    d = _design(fam, cfg, extras)
    s = arm.size(d)
    p = e2.plan(d, s)
    for k in ("config", "bus", "supply", "modules", "custom_pcb", "actuators", "sensors", "power_budget", "issues", "warnings"):
        assert k in p
    assert p["config"] == cfg
    assert p["bus"] == ("pwm" if fam == "hobby_pwm" else "ttl_half_duplex")
    for m in p["modules"]:
        assert m["key"] in catalog.MODULES and m["ref"] and m["role"]
    assert (p["custom_pcb"] is not None) == (cfg == "custom_pcb")
    n_act = sum(int(j.get("count", 1) or 1) for j in s["joints"]) + 1
    assert len(p["actuators"]) == n_act
    assert p["actuators"][-1]["joint"] == "gripper"
    if p["bus"] == "pwm":
        assert [a["channel"] for a in p["actuators"]] == list(range(n_act))
    else:
        ids = [a["id"] for a in p["actuators"]]
        assert len(set(ids)) == len(ids) and all(a["channel"] is None for a in p["actuators"])
    pb = p["power_budget"]
    assert pb["rails"] and all(r["peak_a"] >= r["avg_a"] >= 0 for r in pb["rails"])
    assert isinstance(pb["supply_ok"], bool) and pb["fuse_a"] > 0 and pb["feed_awg"] in range(12, 30)
    assert ("estop" in extras) == any(m["key"] == "estop" for m in p["modules"])
    assert ("wrist_camera" in extras) == any(x["key"] == "wrist_camera" for x in p["sensors"])
    json.dumps(p)


def test_supply_and_voltage_choice():
    p = e2.plan(_design("hobby_pwm", "custom_pcb"), arm.size(_design("hobby_pwm", "custom_pcb")))
    assert p["supply"]["v"] <= 6.0 and p["power_budget"]["supply_ok"]          # SG90 / MG996R stay <= 6 V
    p = e2.plan(_design("smart_bus", "modules"), arm.size(_design("smart_bus", "modules")))
    assert p["supply"]["v"] == 12.0 and p["controller"] == "esp32_s3_devkit"
    p = e2.plan(_design("dynamixel", "modules"), arm.size(_design("dynamixel", "modules")))
    assert p["controller"] == "openrb_150"


# ------------------------------------------------------------------ finalize (all families x configs)

@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("cfg", CONFIGS)
def test_finalize(built, fam, cfg):
    d, s, p, a, od, r = built(fam, cfg, "estop,wrist_camera")
    for k in ("config", "bus", "schematic_svg", "nets", "board", "harness", "firmware", "power_budget", "sourcing", "configs",
              "issues", "warnings"):
        assert k in r
    json.dumps(r)
    # schematic: valid XML with a title block and every component drawn
    root = ET.parse(os.path.join(od, r["schematic_svg"])).getroot()
    assert root.tag.endswith("svg")
    sj = json.load(open(os.path.join(od, "schematic.json")))
    drawn = {sym["ref"] for sym in sj["layout"]["symbols"]}
    assert drawn == {c["ref"] for c in sj["components"]}
    assert r["nets"] and all(len(n["nodes"]) >= 1 and all("." in x for x in n["nodes"]) for n in r["nets"])
    # harness
    assert r["harness"] and all(h["length_mm"] > 0 and h["conductors"] >= 2 for h in r["harness"])
    kinds = {h["kind"] for h in r["harness"]}
    assert ("pwm" if fam == "hobby_pwm" else "servo_bus") in kinds and "sensor" in kinds and "safety" in kinds and "usb" in kinds
    with open(os.path.join(od, "harness.csv")) as fh:
        assert len(list(csv.DictReader(fh))) == len(r["harness"])
    # firmware written for the right library
    code = open(os.path.join(od, r["firmware"])).read()
    lib = {"hobby_pwm": "Adafruit_PWMServoDriver.h", "smart_bus": "SCServo.h", "dynamixel": "Dynamixel2Arduino.h"}[fam]
    assert f"#include <{lib}>" in code and "GRIP_TARGET_N" in code and "enterEstop" in code
    # sourcing rows follow the contract ROW schema
    cats = {"actuator", "electronics", "pcb", "cable", "connector", "fastener", "mechanical", "fabricated"}
    for row in r["sourcing"]:
        assert row["category"] in cats and row["qty"] >= 1 and row["make_or_buy"] in ("buy", "make")
        assert row["vendors"] and row["selected"]["vendor"] and row["ext_price"] >= 0
        assert all(v["url"].startswith("http") for v in row["vendors"])
    assert set(r["configs"]) == {"modules", "custom_pcb"} and all(c["cost"] > 0 and c["summary"] for c in r["configs"].values())
    if cfg == "custom_pcb":
        _check_board(od, r)
        assert any(row["category"] == "pcb" and row["make_or_buy"] == "make" and "JLCPCB" in row["how"] for row in r["sourcing"])
    else:
        assert r["board"] is None


def _check_board(od, r):
    b = r["board"]
    assert b["drc"]["pass"], b["drc"]
    board = json.load(open(os.path.join(od, b["json"])))
    assert board["size_mm"] == b["size_mm"]
    assert board["drc"]["unrouted"] == [] and board["drc"]["disconnected"] == [] and board["drc"]["violations"] == []
    for c in board["components"]:          # Studio animation contract (morph/board.py format)
        assert len(c["outline"]) == 4 and c["pads"] and all({"name", "x", "y", "net"} <= set(p) for p in c["pads"])
    assert board["traces"] and all({"net", "layer", "width", "points"} <= set(t) for t in board["traces"])
    assert board["pour"] and board["ratsnest"]
    assert any(not c["smd"] for c in board["components"]) and any(c["smd"] for c in board["components"])
    assert board["vias"]
    z = os.path.join(od, b["gerbers"])
    assert os.path.exists(z)
    names = zipfile.ZipFile(z).namelist()
    for layer in ("F_Cu", "B_Cu", "F_Mask", "B_Mask", "F_Paste", "F_Silkscreen", "Edge_Cuts", "PTH.drl", "NPTH.drl"):
        assert any(layer in n for n in names), layer
    gdir = os.path.dirname(z)
    for n in names:
        txt = open(os.path.join(gdir, n)).read()
        if n.endswith(".gbr"):
            _check_gerber(txt)
        else:
            assert txt.startswith("M48") and txt.rstrip().endswith("M30")
    with open(os.path.join(od, b["bom"])) as fh:
        rows = list(csv.DictReader(fh))
    assert rows and set(rows[0]) >= {"Comment", "Designator", "Footprint", "LCSC Part #"}
    assert all(re.fullmatch(r"C\d+|verify", row["LCSC Part #"]) for row in rows)
    with open(os.path.join(od, b["cpl"])) as fh:
        cpl = list(csv.DictReader(fh))
    assert cpl and set(cpl[0]) == {"Designator", "Mid X", "Mid Y", "Layer", "Rotation"}
    refs = {c["ref"] for c in board["components"] if c["kind"] != "MH"}
    assert {row["Designator"] for row in cpl} == refs


def _check_gerber(txt):
    assert "%FSLAX46Y46*%" in txt and "%MOMM*%" in txt and txt.rstrip().endswith("M02*")
    defined = set(re.findall(r"%ADD(\d+)", txt))
    used = set(re.findall(r"^D(\d+)\*$", txt, re.M)) - {"01", "02", "03"}
    assert used <= defined
    assert txt.count("G36*") == txt.count("G37*")


def test_extras_change_the_board(built):
    _, _, _, _, od, r = built("smart_bus", "custom_pcb", "imu,estop")
    _check_board(od, r)
    refs = {c["ref"]: c["value"] for c in json.load(open(os.path.join(od, r["board"]["json"])))["components"]}
    assert refs.get("J8") == "IMU" and refs.get("J9") == "E-STOP"
    assert any(h["kind"] == "i2c" for h in r["harness"])


def test_harness_lengths_follow_assembly_cables(built):
    d, s, p, a, od, r = built("smart_bus", "modules", "estop,wrist_camera")
    bus = [c for c in a.cables if (c.meta or {}).get("kind") == "servo_bus"]
    if not bus:
        pytest.skip("assembly has no routed bus cables")
    rows = [h for h in r["harness"] if h["kind"] == "servo_bus"]
    assert [h["length_mm"] for h in rows] == [int(round(a.cable_length(c))) for c in
                                             sorted(bus, key=lambda c: c.meta.get("servo_id", 0))]


# ------------------------------------------------------------------ firmware compiles (syntax) against stub headers

@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("cfg", CONFIGS)
def test_firmware_syntax(tmp_path, fam, cfg):
    for extras in ("", "estop,wifi,imu"):
        d = _design(fam, cfg, extras)
        s = arm.size(d)
        p = e2.plan(d, s)
        a = arm.build(d, s, p)
        rel = e2.firmware(d, p, a, str(tmp_path / extras.replace(",", "_")), "r001")
        src = tmp_path / extras.replace(",", "_") / rel
        cpp = str(src) + ".cpp"
        shutil.copy(src, cpp)
        for defs in ([], ["-DARDUINO_ARCH_ESP32"]):
            res = subprocess.run(["g++", "-std=gnu++17", "-fsyntax-only", "-Wall", "-Wno-unused-variable", f"-I{STUBS}", *defs, cpp],
                                 capture_output=True, text=True)
            assert res.returncode == 0, res.stderr


# ------------------------------------------------------------------ PCB generator unit checks

def test_drc_catches_a_short(built):
    _, _, _, _, od, r = built("hobby_pwm", "custom_pcb", "estop,wrist_camera")
    board = json.load(open(os.path.join(od, r["board"]["json"])))
    from shapely.geometry import Polygon
    pours = [{"layer": x["layer"], "geom": Polygon(x["exterior"], x["holes"])} for x in board["pour"]]
    t = next(t for t in board["traces"] if t["net"] not in ("GND",) and t["layer"] == "F.Cu")
    other = next(t2 for t2 in board["traces"] if t2["net"] not in ("GND", t["net"]) and t2["layer"] == "F.Cu")
    board["traces"].append({"net": t["net"], "layer": "F.Cu", "width": 0.2, "points": [t["points"][0], other["points"][0]]})
    rep = pcb.drc(board, pours)
    assert not rep["pass"] and any(v["rule"] == "clearance" for v in rep["violations"])


def test_footprints_and_usb_crossover_are_clean():
    for name in ("R0603", "C0805", "SOT23_6", "SOT223", "TO252", "SSOP8", "TSSOP28", "ESP32S3_WROOM1", "USBC_16P",
                 "SW_SMD_5x5", "XT30PW", "CONN_2P5_3", "JST_PH_2", "JST_XH_2", "JST_SH_4", "CP_D10", "HDR_3x7", "MH_M3"):
        fp = pcb.footprint(name)
        x0, y0, x1, y1 = fp["court"]
        for p in fp["pads"]:
            assert x0 - 1e-6 <= p["x"] <= x1 + 1e-6 and y0 - 1e-6 <= p["y"] <= y1 + 1e-6, (name, p)
    # pre-routed USB-C D+/D- crossover keeps >= CLEAR to every other pad of the footprint
    from shapely.geometry import LineString
    fp = pcb.footprint("USBC_16P")
    nets = {"A6": "DP", "B6": "DP", "B7": "DM", "A7": "DM", "A1B12": "G", "B1A12": "G", "SH": "G"}
    for t in fp["pre"]:
        g = LineString(t["pts"]).buffer(t.get("w", pcb.CLASSES["sig"]) / 2)
        for p in fp["pads"]:
            if not p["num"] or nets.get(p["num"]) == nets.get(t["pad"]) or p["np"]:
                continue
            if t["layer"] in p["layers"]:
                assert g.distance(pcb._pad_geom(p)) >= pcb.CLEAR - 1e-6, (t["pad"], p["num"])


def test_schematic_renders_every_component():
    d = _design("smart_bus", "custom_pcb", "estop,imu")
    p = e2.plan(d, arm.size(d))
    c = e2.custom_circuit(d, p)
    svg, layout = schematic.render(c, rev="r001", date="2026-01-01")
    ET.fromstring(svg)
    assert {s["ref"] for s in layout["symbols"]} == {x.ref for x in c.comps}
    assert layout["sheet"] in ("A4", "A3", "A2")
    for net in ("V_SERVO", "BUS_DATA", "USB_DP", "FSR_L", "ESTOP_LOOP"):
        assert net in svg
