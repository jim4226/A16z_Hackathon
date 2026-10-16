"""Parts-list lines read the way a reader does (bom._classify), as table data kept in tests/data: what a line is comes
from its part name's head, so a horn, bracket, cable, frame, gear set, power supply or board named after a servo is
not a servo, a model named only where the name says what the part fits ("for", "compatible with", "fits") is what it
fits, and a servo named with what it comes with or how it is built still is one. The STS3215's 12 V model codes
(C018, C047) and spellings (12VDC, 12-volt, 30 kg·cm, Waveshare's ST3215) pick the 12 V servo; a Dynamixel family is
the catalog motor only with its own code. Whole parts lists (SO-101, Koch v1.1, hobby, stepper and BLDC arms, KiCad 8,
KiCad 5-7 grouped, KiBoM, JLC, packs, German headers) seed the motors they list. Offline and fast."""

import io
import json
import os

import pytest

from morph.importers import bom, detect, session as sess

DATA = os.path.join(os.path.dirname(__file__), "data")
with open(os.path.join(DATA, "bom_lines.json"), encoding="utf-8") as _fh:
    LINES = json.load(_fh)                  # the hunters' 418 lines: header, line, catalog key, category (None: any)
with open(os.path.join(DATA, "bom_files", "expect.json"), encoding="utf-8") as _fh:
    FILES = json.load(_fh)                  # file -> {line: [catalog key, motors]}


@pytest.mark.parametrize("row", LINES, ids=[f"{r['group'].split('.')[0]}:{' / '.join(x for x in r['line'] if x)[:44]}"
                                           for r in LINES])
def test_a_parts_list_line_is_the_motor_its_name_names_and_not_the_one_it_fits(row):
    rows, _, _ = bom.map_table([row["header"], row["line"]])
    (r,) = rows
    assert r["catalog"] == row["key"], r
    assert (r["catalog_name"] is None) == (row["key"] is None)
    if row["category"]:
        assert r["category"] == row["category"], r


MORE = [  # (part name, catalog key, pack): a servo named in other ways stays one, and a pack counts its motors
    ("Servo, STS3215", "sts3215_7v4", None), ("Servo (STS3215 12V)", "sts3215", None),
    ("Servo - STS3215 12V", "sts3215", None), ("Motor: STS3215", "sts3215_7v4", None), ("6x STS3215", "sts3215_7v4", 6),
    ("Servo STS3215", "sts3215_7v4", None), ("LeRobot SO-101 STS3215 servo", "sts3215_7v4", None),
    ("XL430 W250", "xl430_w250", None), ("Dynamixel XL430-W250-T (x2)", "xl430_w250", 2), ("MG 996R", "mg996r", None),
    ("SG90 Micro Servo Motor 9G", "sg90", None), ("Micro Servo SG90", "sg90", None), ("TowerPro SG-90", "sg90", None),
    ("STS3215 servo motor kit (6 pcs)", "sts3215_7v4", 6), ("Feetech STS3215 servo motor (C018, 12V)", "sts3215", None),
    ("Waveshare ST3215 Servo 7.4V", "sts3215_7v4", None), ("ST3215-HS servo", None, None), ("XL430-W250-R", None, None),
    ("Servo for the STS3215 arm", None, None), ("Generic 20kg servo", None, None),
    # shop listings that say after the motor word what it is built with or for (all three heads at 04a6469 read
    # "dual", "full", "large" and "robot arm" as the line's head noun: no motor)
    ("MG996R Servo Motor Metal Gear Dual Ball Bearing", "mg996r", None),
    ("DS3218 20kg Digital Servo High Torque Full Metal Gear Waterproof", "ds3218", None),
    ("MG996R Servo Motor RC Robot Arm", "mg996r", None), ("MG996R Servo Large Torque", "mg996r", None),
    # the joint or arm before the model, or before a ":" or a dash (c9f9552's heads read "Shoulder", "Gripper",
    # "Leader arm" as what the line is: no motor; bb1fd6b and 22bec48 tagged each)
    ("Shoulder STS3215 12V", "sts3215", None), ("Koch follower XL430-W250-T", "xl430_w250", None),
    ("Base (J1): STS3215 12V", "sts3215", None), ("Gripper - XL330-M288-T", "xl330_m288", None),
    ("Leader arm: 6x STS3215 7.4V", "sts3215_7v4", 6), ("Axis 1 MG996R", "mg996r", None),
    # a model named first is never a joint label ("SG90" is no "J1"), and the joint after it is ("MG996R: base")
    ("SG90: 2 pcs", "sg90", 2), ("SG90 - gripper", "sg90", None), ("MG996R: base", "mg996r", None),
]


@pytest.mark.parametrize("item, key, pack", MORE, ids=[m[0] for m in MORE])
def test_a_servo_named_in_other_ways_is_still_one(item, key, pack):
    (r,) = bom.map_table([["Part", "Qty", "Notes"], [item, "1", ""]])[0]
    assert (r["catalog"], r["category"], r["pack"]) == (key, "actuator", pack)


NOT_MOTORS = ["MG996R servo ball link", "MG996R servo arm", "STS3215 servo robot arm bracket", "SG90 servo horn set",
              "STS3215 servo bearing", "MG996R servo linkage rod", "Feetech FE-URT-1 servo debug board"]


@pytest.mark.parametrize("item", NOT_MOTORS)
def test_a_part_for_a_servo_named_like_a_listing_is_still_no_servo(item):
    (r,) = bom.map_table([["Part", "Qty", "Notes"], [item, "1", ""]])[0]
    assert r["catalog"] is None and r["category"] != "actuator", r


with open(os.path.join(DATA, "bom_hunt_lines.json"), encoding="utf-8") as _fh:
    HUNT = json.load(_fh)   # the third round's hunters (1313 lines and 55 probes, expectations written before any run)
                            # and the fixer's 71 probes: header, line, catalog key, other keys also right ("accept"),
                            # category (None: any), pack (None: any) and the class of the lines 0033630 read wrong
KNOWN_GAPS = {"Hiwonder LX-16A"}  # a servo the catalog lacks, named by maker and code alone: listed as purchased


def _reads_as_expected(row):
    (r,) = bom.map_table([row["header"], row["line"]])[0]
    accept = [row["key"]] + row["accept"]
    cat = (row["category"] is None or r["category"] == row["category"]
           or r["catalog"] is not None and r["catalog"] in row["accept"]
           or r["catalog"] is None and None in row["accept"])
    return r["catalog"] in accept and cat and (row["pack"] is None or r["pack"] == row["pack"]), r


def _hunt_id(r):
    return f"{r['wrong_at_0033630'] or 'FIXER'}:{' / '.join(x for x in r['line'] if x)[:50]}"


@pytest.mark.parametrize("row", [r for r in HUNT if r["wrong_at_0033630"] or r["group"] == "FX.fixer"], ids=_hunt_id)
def test_a_servo_named_after_its_joint_arm_or_id_keeps_its_motor_and_a_part_named_like_one_doesnt(row):
    """0033630 read 345 of these lines wrong: a label word outside a closed list of joints ("Elbow flex", "Waist", "Left
    arm", "ID 1", "Camera pan"), a label in brackets first ("[J1] STS3215"), a joint reference alone before brackets
    ("J1 (STS3215)"), "DS3218MG", a joint in the part column with the servo in the MPN, "Servo for gripper (SG90)", the
    joint after the model ("STS3215 C018 shoulder pan", "MG996R: upper arm"), packs ("×6 (", "(qty 6)", "set of 6"),
    German joint words, steppers named "NEMA 17" after the motor word, two models in a line, notes about the other arm;
    and joint-named kits and printed parts read as servos ("Pan tilt SG90 kit", "Base (PLA, holds STS3215)")."""
    if row["line"][0] in KNOWN_GAPS:
        pytest.xfail("known gap")
    ok, r = _reads_as_expected(row)
    assert ok, r


def test_every_hunted_parts_list_line_reads_the_way_its_reader_does():
    """The whole round-3 corpus, the lines 0033630 read right too (bracket, accessory, kit, stand-in, two-model and
    German lines among them), so a looser label rule can't turn a horn, frame or kit into a servo unseen."""
    bad = [(" / ".join(x for x in row["line"] if x), row["key"], r["catalog"], r["category"], r["pack"])
           for row in HUNT if row["line"][0] not in KNOWN_GAPS for ok, r in [_reads_as_expected(row)] if not ok]
    assert not bad, bad


PART_NOUNS = ["horn", "bracket", "mount", "plate", "adapter", "board", "driver", "cable", "frame", "idler", "spline",
              "shaft", "coupler", "bearing", "disc", "spacer", "magnet", "sticker", "linkage", "saver", "printed part",
              "pulley", "enclosure", "clamp", "heatsink", "connector", "module", "sensor", "shield", "hat", "splitter",
              "fuse", "capacitor", "regulator", "buck converter", "zip tie", "pliers", "screwdriver", "manual", "guide",
              "gimbal", "omni wheel", "servo arm", "servo horn", "servo shield", "servo hat", "servo wheel",
              "motor mount", "motor driver", "motor coupler"]
FORMS = ["{n} {m}", "{N} {m}", "{N}: {m}", "{m} {n}", "{m}: {n}", "[{N}] {m}", "{N} ({m})", "{N} - {m}", "{N} for {m}",
         "Servo {n} {m}", "{m} servo {n}"]


def test_a_part_in_a_labels_place_is_never_a_servo():
    """A label is open (any words naming the joint, arm or motor), so the words that name other parts are what keeps
    a part a part, in every place a label goes: before the model, after its ":", in brackets first, before brackets or
    a dash, and between the motor word and the model. 0033630 read none of these as a motor; the first open rule
    read 610 of the fixer's 8,790 such lines as motors ("Shield MG996R", "Servo hat SG90", "Splitter: STS3215")."""
    bad = []
    for n in PART_NOUNS:
        for m in ("STS3215", "MG996R", "SG90", "XL430-W250-T", "DS3218"):
            for f in FORMS:
                line = f.format(n=n, N=n.capitalize(), m=m)
                (r,) = bom.map_table([["Part", "Qty", "Notes"], [line, "1", ""]])[0]
                if r["catalog"]:
                    bad.append((line, r["catalog"]))
    assert not bad, bad


@pytest.mark.parametrize("name, want", [
    ("aloha_bimanual.csv", {"xm430_w350": 16, "xl430_w250": 10}), ("motor_ids.csv", {"sts3215_7v4": 6}),
    ("lerobot_joint_names.csv", {"sts3215_7v4": 6}), ("lerobot_model_first.csv", {"sts3215": 6}),
    ("joint_motor_columns.csv", {"mg996r": 3, "sg90": 3}), ("part_desc_joints.csv", {"sts3215": 6}),
    ("packs_mixed.md", {"sts3215": 12, "sts3215_7v4": 6, "mg996r": 6, "sg90": 4})])
def test_the_import_dialog_seeds_the_motors_a_joint_named_list_lists(name, want, tmp_path, monkeypatch):
    """The Import dialog's path (add_file, detect, commit): ALOHA's waist and wrist-angle servos, motor IDs, LeRobot's
    own joint names, the model before the joint, a Joint/Motor table, the joint as the part with the servo described,
    and packs written as shops write them. 0033630 seeded {} for the first two and 4 of 6 for LeRobot's names."""
    h = tmp_path / "projects"
    h.mkdir()
    monkeypatch.setenv("MORPH_HOME", str(h))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    s = sess.Session.create(str(h))
    with open(os.path.join(DATA, "bom_files", name), "rb") as fh:
        data = fh.read()
    s.add_file(name, io.BytesIO(data), len(data))
    c = next(i for i, x in enumerate(s.detect()["candidates"]) if x["kind"] == "bom")
    assert s.commit(c)["actuators"] == want


def _row(res, item):
    if item.startswith("#"):                # KiBoM: the line's group number
        return res["rows"][int(item[1:]) - 1]
    return next(r for r in res["rows"] if r["item"] == item)


@pytest.mark.parametrize("name", sorted(FILES))
def test_a_whole_parts_list_seeds_the_motors_it_lists(name):
    path = os.path.join(DATA, "bom_files", name)
    assert detect.sniff(path) in ("csv", "markdown"), name           # a KiCad grouped BOM's "Source:" rows too
    res = bom.parse(path)
    want = {}
    for item, (key, n) in FILES[name]["lines"].items():
        assert _row(res, item)["catalog"] == key, (name, item, _row(res, item))
        if key:
            want[key] = want.get(key, 0) + n
    assert bom.actuator_keys(res["rows"]) == want, name


def test_kicad_parts_lists_name_their_parts_by_value_and_connectors_are_never_motors():
    """KiCad 5-7's grouped CSV (its "Qnty" column after five "Source:" rows), KiCad 8's Symbol Fields table and KiBoM
    (whose "Component" column is the group number): the part is its value; a JST connector labelled for the STS3215
    bus (J2-J7) is no servo, the Motor_Servo symbols (M1, M2) are."""
    legacy = bom.parse(os.path.join(DATA, "bom_files", "servo_board_kicad_legacy.csv"))
    assert legacy["columns"]["qty"] == "Qnty" and legacy["columns"]["item"] == "Value"
    assert [(r["item"], r["qty"], r["catalog"]) for r in legacy["rows"]] == [
        ("100nF", 2, None), ("STS3215", 6, None), ("MG996R", 2, "mg996r"), ("CH340C", 1, None)]
    assert _row(legacy, "STS3215")["category"] == "electronics"
    kibom = bom.parse(os.path.join(DATA, "bom_files", "servo_board_kibom.csv"))
    assert kibom["columns"]["item"] == "Value" and [r["item"] for r in kibom["rows"]] == [
        "100nF", "SG90", "CH340C", "STS3215 bus"]
    kicad8 = bom.parse(os.path.join(DATA, "bom_files", "servo_board_kicad8.csv"))
    assert _row(kicad8, "STS3215_BUS")["catalog"] is None and _row(kicad8, "SG90")["catalog"] == "sg90"


def test_joint_references_in_a_hand_made_list_are_not_connectors():
    """A hand-made arm list whose "Ref" column names the joints it drives ("J1-J6", "J1 J2") keeps its servos: only a
    board's parts list (one with a footprint column) reads J as a connector and U as a chip."""
    rows, _, _ = bom.map_table([["Ref", "Part", "Qty"], ["J1-J6", "Feetech STS3215 12V servo", "6"],
                                ["J1 J2", "STS3215 servo", "2"], ["U1", "Waveshare bus servo adapter", "1"]])
    assert [(r["catalog"], r["category"]) for r in rows] == [("sts3215", "actuator"), ("sts3215_7v4", "actuator"),
                                                             (None, "electronics")]
    board, _, _ = bom.map_table([["Reference", "Value", "Footprint", "Qty"],
                                 ["J1 J2", "STS3215 servo", "Connector_JST:JST_EH_B3B-EH-A_1x03_P2.50mm_Vertical", "2"]])
    assert (board[0]["catalog"], board[0]["category"]) == (None, "electronics")


def test_a_parts_list_without_prices_has_no_total(tmp_path):
    """Not "Total $0.00 (from the listed prices)": with no price on any line there is no total to show."""
    for name in ("servo_board_kicad8.csv", "servo_board_kibom.csv", "jlc_bom.csv"):
        res = bom.parse(os.path.join(DATA, "bom_files", name))
        assert res["total"] is None and any("have no price" in w for w in res["warnings"]), name
    assert bom.parse(os.path.join(DATA, "bom_files", "hobby_arm.csv"))["total"] == pytest.approx(67.6)


def test_the_import_dialog_reads_a_kicad_grouped_bom(tmp_path, monkeypatch):
    """The dialog's detect sees the KiCad 5-7 grouped CSV as a parts list (it said "Morph found nothing it can import
    here"), and its commit seeds the two MG996R servos only."""
    h = tmp_path / "projects"
    h.mkdir()
    monkeypatch.setenv("MORPH_HOME", str(h))
    from morph.importers import common
    monkeypatch.setattr(common, "_HOME", None)
    s = sess.Session.create(str(h))
    with open(os.path.join(DATA, "bom_files", "servo_board_kicad_legacy.csv"), "rb") as fh:
        data = fh.read()
    s.add_file("servo_board.csv", io.BytesIO(data), len(data))
    (c,) = s.detect()["candidates"]
    assert c["kind"] == "bom" and c["count"] == 4 and c["total"] is None
    res = s.commit(0)
    assert res["actuators"] == {"mg996r": 2} and res["total"] is None
