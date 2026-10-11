"""Darwin + Keewano inside the whole-robot Studio, the demo reset and the time card; offline."""

import json
import os
from urllib.error import URLError

from morph import darwin, keewano
from morph.pipeline import Project
from morph.robot import timestudy2

ROBOT_RESULTS = {
    "parent": None,
    "sizing": {},
    "bom": {"rows": [
        {"item": "Feetech STS3215 serial bus servo (12 V, 30 kg·cm)", "category": "actuator", "make_or_buy": "buy", "ext_price": 98.0},
        {"item": "ESP32-S3-DevKitC-1", "category": "electronics", "make_or_buy": "buy", "ext_price": 15.0},
        {"item": "Crimp kit: JST-PH/XH/SH housings + contacts, heat-shrink", "category": "connector", "make_or_buy": "buy", "ext_price": 9.0},
        {"item": "Molex 5264 3p (2.5 mm) servo cable 100 mm", "category": "cable", "make_or_buy": "buy", "ext_price": 0.6},
        {"item": "Molex 5264 3p (2.5 mm) servo cable 200 mm", "category": "cable", "make_or_buy": "buy", "ext_price": 0.6},
        {"item": "M3×8 flat head screw", "category": "fastener", "make_or_buy": "buy", "ext_price": 0.1},
        {"item": "M2.5×6 socket head cap screw", "category": "fastener", "make_or_buy": "buy", "ext_price": 0.3},
        {"item": "Feetech STS3215 serial bus servo horn + horn screw", "category": "mechanical", "make_or_buy": "buy", "ext_price": 0.0},
        {"item": "Base shell", "category": "fabricated", "make_or_buy": "make", "ext_price": 30.9},
    ]},
    "manufacturing": {"parts": [
        {"group": "base_shell", "process": "fdm_print", "files": ["base_shell/base_shell.stl", "base_shell/base_shell.3mf"]},
        {"group": "ua_plate", "process": "cnc_mill", "files": ["ua_plate/ua_plate.dxf", "ua_plate/ua_plate.nc"]},
        {"group": "bracket", "process": "sheet_metal", "files": ["bracket/bracket_flat.dxf", "bracket/bracket.stl"]},
    ]},
}


def _robot_project(tmp_path, results=ROBOT_RESULTS, rev="r001"):
    root = tmp_path / "egg-picker"
    (root / rev).mkdir(parents=True)
    (root / rev / "results.json").write_text(json.dumps(results))
    return Project(str(root))


def test_robot_bom_becomes_darwin_tasks(tmp_path):
    p = _robot_project(tmp_path)
    tasks = darwin.tasks_for(ROBOT_RESULTS, p.rev_dir("r001"))
    parts = [t for t in tasks if t[0] == "part"]
    queries = [t[2] for t in parts]
    assert "buy Feetech STS3215 serial bus servo, price and shipping" in queries  # no "(12 V, 30 kg·cm)"
    assert "buy Crimp kit JST-PH/XH/SH housings + contacts, price and shipping" in queries
    assert [t[1] for t in parts].count("Servo cables") == 1  # all cable lengths are one lookup
    assert [t[1] for t in parts].count("Fastener kit (M2-M4)") == 1
    assert not any("Base shell" in t[1] or "horn" in t[1] for t in parts)  # made in-house / free
    kinds = {t[0]: t[3] for t in tasks if t[0] not in ("part", "quote", "order")}
    assert kinds["print"] == ["mfg/base_shell/base_shell.stl", "mfg/base_shell/base_shell.3mf"]
    assert kinds["cnc"] == ["mfg/ua_plate/ua_plate.dxf", "mfg/ua_plate/ua_plate.nc"]
    assert kinds["sheet_metal"] == ["mfg/bracket/bracket_flat.dxf"]
    assert "design_review" in kinds and "pcb_fab" not in kinds  # no Gerbers in a modules build


def test_robot_source_streams_progress_and_compacts_for_the_studio(tmp_path, monkeypatch):
    from morph.studio.server import _sourcing

    p = _robot_project(tmp_path)
    monkeypatch.setattr(keewano, "emit", lambda *a, **k: False)
    monkeypatch.setattr(darwin, "search", lambda q, n=5, **kw: {"query": q, "ok": True, "agents": [] if "Crimp" in q else [
        {"name": "shopping_search", "agent_id": "cmuds975i00op0agm4ievg1pe", "can_start": False, "url": "u", "description": "d" * 400}]})
    seen = []
    rep = darwin.source(p, "r001", on_progress=lambda n, tot, t: seen.append((n, tot, t["kind"])), timeout=5)
    assert len(seen) == len(rep["rows"]) and seen[-1][0] == seen[-1][1]
    assert ("part", "Crimp kit: JST-PH/XH/SH housings + contacts, heat-shrink", "no_agent") in {(g["kind"], g["item"], g["gap"]) for g in rep["gaps"]}
    sc = _sourcing(p, "r001")
    assert sc["rev"] == "r001" and len(sc["rows"]) == len(rep["rows"])
    row = next(r for r in sc["rows"] if r["item"] == "ESP32-S3-DevKitC-1")
    assert row["picked"][0]["name"] == "shopping_search" and len(row["picked"][0]["description"]) <= 160
    json.dumps(sc)


def test_darwin_falls_back_to_the_shipped_cache(tmp_path, monkeypatch):
    q = "buy Feetech STS3215 serial bus servo, price and shipping"
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / os.path.basename(darwin._cache_path(q))).write_text(json.dumps({"query": q, "ok": True, "agents": [{"name": "x"}]}))
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "empty"))
    monkeypatch.setattr(darwin, "SHIPPED", str(shipped))

    def down(*a, **k):
        raise URLError("offline")

    monkeypatch.setattr(darwin, "urlopen", down)
    monkeypatch.setattr(darwin.time, "sleep", lambda s: None)
    out = darwin.search(q)
    assert out["cached"] is True and out["agents"][0]["name"] == "x"


def test_repo_ships_a_darwin_cache_for_the_demo():
    files = os.listdir(darwin.SHIPPED)
    assert len([f for f in files if f.endswith(".json")]) >= 10
    for f in files:
        with open(os.path.join(darwin.SHIPPED, f)) as fh:
            d = json.load(fh)
        assert d["ok"] and "query" in d and "agents" in d


def _rev(root, rev, parent, label=""):
    (root / rev).mkdir(parents=True)
    (root / rev / "results.json").write_text(json.dumps({"parent": parent, "label": label, "provenance": {}, "timings_s": {}}))


def test_demo_reset_keeps_the_last_full_build(tmp_path):
    from morph.robot.pipeline2 import RobotProject
    from morph.studio.server import Studio

    home = tmp_path / "projects"
    egg, arm = home / "egg-picker", home / "so101"
    for r, parent in (("r001", None), ("r002", "r001"), ("r003", None), ("r004", "r003"), ("r005", "r004")):
        _rev(egg, r, parent)
    for r, parent in (("r001", None), ("r002", "r001")):
        _rev(arm, r, parent)
    st = Studio(RobotProject(str(egg)), str(home))
    st.p = RobotProject(str(arm))  # the presenter had switched to so101
    assert st.reset()
    assert RobotProject(str(egg)).revs() == ["r001", "r002", "r003"]
    assert RobotProject(str(arm)).revs() == ["r001"]
    assert os.path.basename(st.p.root) == "egg-picker"


def test_time_card_counts_a_rebuilt_design_once(tmp_path):
    from morph.robot.pipeline2 import RobotProject

    root = tmp_path / "egg"
    _rev(root, "r001", None, "first design")
    _rev(root, "r002", "r001", "Swap to Dynamixel servos")
    _rev(root, "r003", None, "rebuilt with latest Morph")
    card = timestudy2.project(RobotProject(str(root)))
    assert [r["rev"] for r in card["revisions"]] == ["r001", "r002"]


def test_env_file_sets_missing_keys_only(tmp_path, monkeypatch):
    from morph.cli import load_env

    f = tmp_path / ".env"
    f.write_text('# keys\nKEEWANO_PAT="kwpat-abc"\nexport KEEWANO_PROJECT_ID=p1\nANTHROPIC_API_KEY=\nKEEWANO_API_KEY=from-file\n')
    monkeypatch.delenv("KEEWANO_PAT", raising=False)
    monkeypatch.delenv("KEEWANO_PROJECT_ID", raising=False)
    monkeypatch.setenv("KEEWANO_API_KEY", "from-terminal")
    assert sorted(load_env(str(f))) == ["KEEWANO_PAT", "KEEWANO_PROJECT_ID"]
    assert os.environ["KEEWANO_PAT"] == "kwpat-abc" and os.environ["KEEWANO_API_KEY"] == "from-terminal"
