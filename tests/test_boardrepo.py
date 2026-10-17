"""KiCad export + BoardRepo references, offline: no network, no KiCad needed (kicad-cli checks skip without it)."""

import json
import os
import re
import shutil
import subprocess
import zipfile

import pytest

from morph import board, boardrepo, cli, darwin, kicad

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
    assert " thru_hole rect " not in pcb   # round pads, as Morph's Gerbers and pour assume


def test_zone_is_stored_filled_with_morphs_pour():
    from shapely.geometry import Polygon
    b = _example()
    pcb = kicad.to_kicad_pcb(b, "egg")
    assert pcb.count("(filled_polygon ") == len(b["pour"])
    for ring in b["pour"]:
        fr = kicad._fracture(ring["exterior"], ring["holes"])
        area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(fr, fr[1:] + fr[:1]))) / 2
        assert area == pytest.approx(Polygon(ring["exterior"], ring["holes"]).area, rel=1e-9)


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
    assert "LICENSE" not in r1["files"] and not (tmp_path / "a" / "LICENSE").exists()  # the board is the user's design
    readme = (tmp_path / "a" / "README.md").read_text()
    assert "| Servo | mg996r |" in readme and "## Related" in readme and "Morph DRC | pass" in readme
    assert "No licence is included" in readme and "Apache" not in readme


def test_a_licence_is_added_only_when_the_user_picks_one(tmp_path):
    b = _example()
    r = kicad.export(b, str(tmp_path / "a"), "egg-r004", license=kicad.license_text("apache-2.0"))
    assert "LICENSE" in r["files"] and "Apache License" in (tmp_path / "a" / "LICENSE").read_text(encoding="utf-8")
    assert "Licensed under Apache-2.0 (LICENSE)." in (tmp_path / "a" / "README.md").read_text(encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "a" / r["zip"]) as z:
        assert "egg-r004/LICENSE" in z.namelist()
    own = tmp_path / "MY-LICENSE.txt"
    own.write_text("CERN-OHL-S-2.0 text", encoding="utf-8")
    kicad.export(b, str(tmp_path / "b"), "egg-r004", license=kicad.license_text(str(own)))
    assert (tmp_path / "b" / "LICENSE").read_text(encoding="utf-8") == "CERN-OHL-S-2.0 text"
    assert "Licensed under the terms in LICENSE." in (tmp_path / "b" / "README.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        kicad.license_text("gpl-please")


def test_pipeline_board_exports_carry_no_morph_licence(tmp_path):
    out = board.build({"mcu": "arduino_nano", "sensing": {"divider_ohm": 3900}}, str(tmp_path))
    assert "kicad/LICENSE" not in out["files"] and not (tmp_path / "kicad" / "LICENSE").exists()
    with zipfile.ZipFile(tmp_path / "kicad" / "board-kicad.zip") as z:
        assert not [n for n in z.namelist() if n.endswith("LICENSE")]


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


def _kicad_cli_major():
    exe = shutil.which("kicad-cli")
    try:
        return int(subprocess.run([exe, "version"], capture_output=True, text=True, timeout=30).stdout.split(".")[0]) if exe else 0
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0


@pytest.mark.skipif(_kicad_cli_major() < 8, reason="needs kicad-cli 8+ (the project is in the KiCad 8/9 format)")
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
    assert boardrepo.parse_md("\ufeff" + MD.replace("\n", "\r\n")) == info   # BOM / CRLF
    assert boardrepo.parse_md("no front matter")["license"] is None


def test_board_ids_accept_pasted_urls_and_reject_the_rest():
    for s in ("evroc/robotic-gripper", "https://boardrepo.com/evroc/robotic-gripper/",
              "https://boardrepo.com/evroc/robotic-gripper.md", "boardrepo.com/evroc/robotic-gripper"):
        assert boardrepo.board_id(s) == "evroc/robotic-gripper"
    for s in ("?q=1#", "../x", "a b/c", "evroc", "a/b/c"):
        assert boardrepo.board_id(s) is None
    assert boardrepo.fetch("?q=1#")["ok"] is False   # rejected before any request


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


class _Resp:
    def __init__(self, text):
        self.text = text.encode()

    def read(self, n=-1):
        return self.text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_keeps_a_live_answer_when_the_cache_cannot_be_written(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(boardrepo, "CACHE", str(blocker / "cache"))
    monkeypatch.setattr(boardrepo, "urlopen", lambda *a, **k: _Resp(MD))
    info = boardrepo.fetch("https://boardrepo.com/evroc/robotic-gripper")
    assert info["ok"] is True and info["license"] == "MIT" and info["board"] == "evroc/robotic-gripper"


def test_morph_kicad_checks_project_and_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="no project nosuch"):
        cli.main(["kicad", "nosuch"])
    assert not (tmp_path / "nosuch").exists()   # no side effect
    (tmp_path / "egg" / "r001").mkdir(parents=True)
    with pytest.raises(SystemExit, match="no revision r999"):
        cli.main(["kicad", "egg", "r999"])
    with pytest.raises(SystemExit, match="no carrier board"):
        cli.main(["kicad", "egg", "r001"])



def test_morph_kicad_refuses_an_unknown_licence_before_writing(tmp_path, monkeypatch):
    from morph import keys
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(cli, "HOME", str(tmp_path))
    board_dir = tmp_path / "egg" / "r001" / "electronics" / "board"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="--license takes apache-2.0 or the path of a licence file"):
        cli.main(["kicad", "egg", "r001", "--license", "gpl-please"])
    assert not (tmp_path / "egg" / "r001" / "boardrepo").exists()

def test_darwin_pcb_handoff_carries_the_kicad_zip(tmp_path):
    bdir = tmp_path / "electronics" / "board"
    (bdir / "gerbers").mkdir(parents=True)
    (bdir / "gerbers" / "gerbers.zip").write_bytes(b"")
    pcb = lambda: [h for h in darwin.handoffs(str(tmp_path), {}) if h[0] == "pcb_fab"][0]  # noqa: E731
    before = pcb()
    (bdir / "kicad").mkdir()
    (bdir / "kicad" / "board-kicad.zip").write_bytes(b"")
    after = pcb()
    assert after[1] == before[1]   # same Darwin query, so its cache still hits
    assert after[2] == before[2] + ["electronics/board/kicad/board-kicad.zip"]   # JSON paths use '/' everywhere
