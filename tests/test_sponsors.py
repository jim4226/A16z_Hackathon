"""Keewano + Darwin integration, offline: no network, no credentials."""

import json
import os
import shutil

from morph import darwin, electronics, keewano
from morph.pipeline import Project

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "raw-egg")


def test_revision_events_carry_design_context_and_resolved_servo():
    ev = dict(keewano.events_for({
        "type": "revision", "rev": "r002", "recomputed": ["sim"],
        "design": {"finger": {"pad": "contour", "material": "PETG"}, "electronics": {"servo": "auto"},
                   "object": {"name": "raw egg"}},
        "predicted": {"min_hold_force_n": 0.8, "target_force_n": 2.3, "servo": "mg996r"}}))
    assert ev["PadStyle"] == "contour" and ev["Material"] == "PETG"
    assert ev["Servo"] == "mg996r"  # not "auto"
    assert ev["PredictedMinHoldMilliN"] == 800 and ev["TargetForceMilliN"] == 2300


def test_measurement_emits_ratio_and_skips_missing_values():
    ev = dict(keewano.events_for(
        {"type": "measurement", "rev": "r002", "actual": {"min_hold_force_n": 1.2},
         "error_vs_prediction": {"min_hold_force_n": 0.4}},
        {"finger": {"pad": "flat", "material": "PLA"}}))
    assert ev["HoldForceRatioPct"] == 150  # 1.2 measured / 0.8 predicted
    assert "GraspSuccessPct" not in ev
    assert all(v is not None for v in ev.values())


def test_every_event_is_registered():
    names = {e["name"] for e in keewano.CUSTOM_EVENTS}
    ids = [e["eventTypeId"] for e in keewano.CUSTOM_EVENTS]
    assert len(set(ids)) == len(ids) and min(ids) >= 2000
    for entry in ({"type": "human_time", "step": "test", "minutes": 4},
                  {"type": "tool_time", "cmd": "new", "seconds": 25.3},
                  {"type": "exploration", "candidates": {"flat": "r001", "contour": "r003"}, "recommended": "r003"},
                  {"type": "sourcing", "part": "FSR 402", "found": 0, "gap": "no_agent"}):
        for name, _ in keewano.events_for(entry):
            assert name in names


def test_offline_is_a_no_op(monkeypatch):
    for k in ("KEEWANO_API_KEY", "KEEWANO_PAT", "KEEWANO_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert keewano.send("p", [("Revision", "r001")]) is False
    assert keewano.query("SELECT 1") is None


def _plan_target(monkeypatch, ratio):
    monkeypatch.setattr(keewano, "hold_ratio", lambda pad, mat: ratio)
    p = Project(EXAMPLE)
    r = p.load_results("r001")
    return electronics.plan(p.load_design("r001"), r["analysis"], r["sim"])


def test_keewano_calibration_raises_target(monkeypatch):
    base = _plan_target(monkeypatch, None)
    cal = _plan_target(monkeypatch, {"ratio": 1.4, "n": 9, "source": "keewano"})
    assert base["hold_calibration"] is None
    assert cal["hold_calibration"]["n"] == 9
    assert cal["target_force_n"] > base["target_force_n"]
    assert cal["hold_force_n"] > base["hold_force_n"]


def test_darwin_source_shortlists_parts_and_reports_gaps(tmp_path, monkeypatch):
    root = tmp_path / "raw-egg"
    shutil.copytree(EXAMPLE, root)
    monkeypatch.setattr(keewano, "emit", lambda *a, **k: False)

    def fake_search(query, max_results=5):
        if "FSR" in query:
            return {"query": query, "ok": True, "agents": []}
        if "Gerber" in query:
            return {"query": query, "ok": False, "agents": [], "error": "HTTPError 500"}
        return {"query": query, "ok": True, "agents": [
            {"name": "other_shop", "agent_id": "x1", "can_start": False, "readiness": "ready", "url": "u"},
            {"name": "shopping_search", "agent_id": "cmuds975i00op0agm4ievg1pe", "can_start": "Nano" in query,
             "readiness": "ready", "url": "u"}]}

    monkeypatch.setattr(darwin, "search", fake_search)
    p = Project(str(root))
    rep = darwin.source(p, "r001")
    items = {r["item"]: r for r in rep["rows"]}
    assert not any("fdm_print" in i for i in items)  # printed parts are ours, not bought
    gaps = {(g["item"], g["gap"]) for g in rep["gaps"]}
    assert ("Interlink FSR 402", "no_agent") in gaps
    assert ("pcb_fab", "darwin_unavailable") in gaps
    assert items["Arduino Nano (ATmega328P)"]["gap"] is None
    assert [r["kind"] for r in rep["rows"][:3]] == ["quote", "quote", "order"]
    assert items["TowerPro MG996R (standard, metal gear)"]["readiness_verified"] is False
    assert items["Arduino Nano (ATmega328P)"]["readiness_verified"] is True
    nano = items["Arduino Nano (ATmega328P)"]
    assert nano["agents"][0]["name"] == "shopping_search"  # preferred agent sorted ahead of x1
    assert [a["name"] for a in nano["picked"]] == ["shopping_search"]
    logged = [e for e in p.entries() if e["type"] == "sourcing"]
    assert len(logged) == len(rep["rows"])
    assert json.load(open(root / "r001" / "sourcing_darwin.json"))["note"].startswith("Shortlist only")


def test_darwin_normalize_reads_live_shape():
    raw = {"searchId": "s1", "response": {"agents": [
        {"name": "upload_rfq_file", "readiness": "unavailable", "canStartThread": False,
         "agentId": "a1", "capabilityId": "c1", "reasons": ["takes Gerbers"]}]}}
    out = darwin._normalize("q", raw)
    a = out["agents"][0]
    assert a["can_start"] is False and a["url"].endswith("/agent/a1?capability=c1")
