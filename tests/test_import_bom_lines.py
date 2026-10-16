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
]


@pytest.mark.parametrize("item, key, pack", MORE, ids=[m[0] for m in MORE])
def test_a_servo_named_in_other_ways_is_still_one(item, key, pack):
    (r,) = bom.map_table([["Part", "Qty", "Notes"], [item, "1", ""]])[0]
    assert (r["catalog"], r["category"], r["pack"]) == (key, "actuator", pack)


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
