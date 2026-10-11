"""KiCad export + BoardRepo references, offline: no network, no KiCad needed (kicad-cli checks skip without it)."""

import json
import os
import re
import shutil
import zipfile

import pytest

from morph import board, boardrepo, darwin, kicad

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "raw-egg", "r004", "electronics", "board", "board.json")


def _example():
    with open(EXAMPLE) as fh:
        return json.load(fh)


def _balanced(text):
    depth = 0
    for ch in re.sub(r'"(?:\\.|[^"\\])*"', "", text):   # parens inside strings don't count
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def test_pcb_has_the_same_copper_as_board_json():
    b = _example()
    pcb = kicad.to_kicad_pcb(b, "egg")
    assert pcb.startswith("(kicad_pcb") and _balanced(pcb)
    pads = [p for c in b["components"] for p in c["pads"]]
    assert pcb.count("\t(footprint ") == len(b["components"])
    assert pcb.count(" thru_hole ") == len(pads)
    assert pcb.count("\t(segment ") == sum(len(t["points"]) - 1 for t in b["traces"])
    assert pcb.count("\t(zone ") == len(b["pour"])
    for net in b["nets"]:
        assert f'(net {list(b["nets"]).index(net) + 1} "{net}")' in pcb
    assert '(sheetfile "egg.kicad_sch")' in pcb
    assert "(via " not in pcb   # the router never needs vias; the KiCad file adds none of its own


def test_schematic_labels_every_connected_pin():
    b = _example()
    sch = kicad.to_kicad_sch(b, "egg")
    assert sch.startswith("(kicad_sch") and _balanced(sch)
    pads = [p for c in b["components"] for p in c["pads"]]
    assert sch.count("\t(global_label ") == sum(1 for p in pads if p.get("net"))
    assert sch.count("\t(no_connect ") == sum(1 for p in pads if not p.get("net"))


def test_export_is_deterministic_and_zips_the_folder(tmp_path):
    b = _example()
    r1 = kicad.export(b, str(tmp_path / "a"), "egg-r004", {"Servo": "mg996r"}, "## Related\n")
    kicad.export(b, str(tmp_path / "b"), "egg-r004", {"Servo": "mg996r"}, "## Related\n")
    for f in r1["files"]:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    with zipfile.ZipFile(tmp_path / "a" / r1["zip"]) as z:
        assert sorted(z.namelist()) == sorted(f"egg-r004/{f}" for f in r1["files"])
    readme = (tmp_path / "a" / "README.md").read_text()
    assert "| Servo | mg996r |" in readme and "## Related" in readme and "Morph DRC | pass" in readme


def test_board_build_writes_kicad_next_to_the_gerbers(tmp_path):
    out = board.build({"mcu": "arduino_nano", "sensing": {"divider_ohm": 3900}}, str(tmp_path))
    assert "error" not in out["kicad"]
    for f in ("kicad/board.kicad_pcb", "kicad/board.kicad_sch", "kicad/board.kicad_pro", "kicad/board-kicad.zip"):
        assert f in out["files"] and (tmp_path / f).exists()
    assert (tmp_path / "gerbers" / "gerbers.zip").exists()


def test_a_writer_failure_never_costs_the_gerbers(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad board")
    monkeypatch.setattr(kicad, "export", boom)
    out = board.build({"mcu": "arduino_nano", "sensing": {"divider_ohm": 3900}}, str(tmp_path))
    assert out["kicad"] == {"error": "ValueError: bad board"}
    assert "gerbers/gerbers.zip" in out["files"] and out["drc"]["pass"]


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")
@pytest.mark.parametrize("mcu", ["arduino_nano", "rp2040_pico", "esp32_devkit"])
def test_kicad_drc_erc_and_parity_are_clean(tmp_path, mcu):
    board.build({"mcu": mcu, "sensing": {"divider_ohm": 10000}}, str(tmp_path))
    res = kicad.check(str(tmp_path / "kicad"), "board")
    assert res["ok"], res


def test_check_without_kicad_cli_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert kicad.check(str(tmp_path), "board") is None


MD = """---
title: "robotic-gripper"
description: "Cheap, 3d printed gripper for robotic applications"
url: "https://boardrepo.com/evroc/robotic-gripper"
board: "evroc/robotic-gripper"
license: "MIT"
---

# robotic-gripper

## Contents

- Schematic
- PCB layout
- Bill of materials

## Files
"""


def test_parse_markdown_twin():
    info = boardrepo.parse_md(MD)
    assert info["license"] == "MIT" and info["title"] == "robotic-gripper"
    assert info["contents"] == ["Schematic", "PCB layout", "Bill of materials"]
    assert boardrepo.parse_md("no front matter")["license"] is None


def test_references_follow_the_design():
    egg = {"finger": {"sensor": "fsr402"}, "electronics": {"mcu": "auto", "servo": "mg996r"},
           "arm": {"actuator_family": "smart_bus"}}
    gripper = {r["board"] for r in boardrepo.related(boardrepo.keys_for(egg, {"servo": "mg996r"}))}
    assert "evroc/robotic-gripper" in gripper and "szbeni/forcegauge" in gripper
    assert "pollen-robotics/elec-rpi-robot-hat" not in gripper   # arm defaults don't count for a gripper project
    arm = {r["board"] for r in boardrepo.related(boardrepo.keys_for(egg, robot=True))}
    assert "pollen-robotics/elec-rpi-robot-hat" in arm
    md = boardrepo.notes_md(boardrepo.related({"smart_bus"}))
    assert "no licence recognised: link only" in md


def test_fetch_offline_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(boardrepo, "CACHE", str(tmp_path))

    def down(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(boardrepo, "urlopen", down)
    info = boardrepo.fetch("evroc/robotic-gripper")
    assert info["ok"] is False and "network down" in info["error"]
    assert info["url"] == "https://boardrepo.com/evroc/robotic-gripper"


def test_darwin_pcb_handoff_carries_the_kicad_zip(tmp_path):
    bdir = tmp_path / "electronics" / "board"
    (bdir / "gerbers").mkdir(parents=True)
    (bdir / "gerbers" / "gerbers.zip").write_bytes(b"")
    (bdir / "kicad").mkdir()
    (bdir / "kicad" / "board-kicad.zip").write_bytes(b"")
    pcb = [h for h in darwin.handoffs(str(tmp_path), {}) if h[0] == "pcb_fab"][0]
    assert pcb[1] == "PCB fabrication and assembly quote from Gerber files, 2-layer through-hole"  # cache key unchanged
    assert pcb[2] == [os.path.join("electronics", "board", "gerbers", "gerbers.zip"),
                      os.path.join("electronics", "board", "kicad", "board-kicad.zip")]
