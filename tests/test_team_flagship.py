"""The Teams flagship workflow's server side, fast (the browser walk-through is tests/e2e/test_e2e_teams_flagship.py):
the change-request preview in numbers (teams/impact.py) from the pipeline's real fast stages, nobody approving their own
work above prototype (and the page being told why), a removed member's open page ending, and the copy along the way."""

import http.client
import json
import os
import time

import pytest

from morph import analysis
from morph import electronics as grip_electronics
from morph.design import build_profile
from morph.robot import arm, electronics2
from morph.robot.catalog import ACTUATORS
from morph.robot.pipeline2 import ORDER, RobotProject, gripper_actuator
from morph.spec import Design
from morph.studio import api_team, server
from morph.studio.teams import activity, auth, collab, impact
from morph.studio.teams import cli as team_cli
from test_studio_features import caller, start
from test_team_util import build, make_team, members, request

TEAM = (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["electronics"]), ("Raj", "editor", ["manufacturing"]),
        ("Vic", "viewer", []))


@pytest.fixture(autouse=True)
def clean():
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield
    api_team.configure(home=None, enabled=False)


def real_base(root, rev="r001", changes=None, unit_cost=300.0, by=None):
    """A revision whose sizing, gripper force and electronics plan are the pipeline's own (the fast stages), so the
    preview compares real numbers; the slow stages (CAD, simulation, manufacturing) are left out."""
    d = Design().with_changes({"object.name": "raw egg", "object.mass_g": 60.0, "object.max_force_n": 25.0,
                               "object.diameter_mm": 44.0, "object.length_mm": 58.0, "arm.reach_mm": 320.0, **(changes or {})})
    sz = arm.size(d)
    ga = gripper_actuator(d)
    gd = d.with_changes({"electronics.servo": ga, "electronics.supply_v": ACTUATORS[ga]["nominal_v"]})
    force = grip_electronics.plan(gd, analysis.analyze(gd, build_profile(gd)), None)
    ep = electronics2.plan(d, {**sz, "grip_target_n": force["target_force_n"]})
    os.makedirs(os.path.join(root, rev), exist_ok=True)
    if not os.path.exists(os.path.join(root, "meta.json")):
        with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"kind": "robot"}, fh)
    res = {"rev": rev, "parent": None, "kind": "robot", "sizing": sz, "eplan": ep, "gripper": {"force": force},
           "provenance": {s: "computed" for s in ORDER},
           "summary": {"unit_cost": unit_cost, "min_torque_margin": min(j["margin"] for j in sz["joints"]), "feasible": True, "issues": []},
           "bom": {"totals": {"total": unit_cost, "per_config": {"prototype": unit_cost, "pilot": unit_cost * 1.8,
                                                                  "production": unit_cost * 40}}},
           "electronics": {"configs": {"modules": {"cost": 100.0}, "custom_pcb": {"cost": 116.0}}}}
    for k in [k for s in ORDER for k in RobotProject._keys(s)]:
        res.setdefault(k, {"computed_in": rev})
    with open(os.path.join(root, rev, "design.json"), "w", encoding="utf-8") as fh:
        json.dump(d.to_dict(), fh)
    with open(os.path.join(root, rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    with open(os.path.join(root, "ledger.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-10-13T08:00:00Z", "type": "revision", "rev": rev, "by": by, "design": d.to_dict()}) + "\n")
    return d, res


def rows(out) -> dict:
    return {r["key"]: r for r in out["rows"]}


# ------------------------------------------------------------------ the preview, in numbers


def test_a_servo_swap_preview_shows_torque_margins_wiring_firmware_and_cost(tmp_path):
    root = str(tmp_path / "egg-picker")
    _, base = real_base(root)
    t0 = time.time()
    out = impact.impact(root, "r001", {"arm.actuator_family": "dynamixel"})
    # well under a second here; the bound only catches a preview that started building (CAD, simulation, manufacturing)
    assert time.time() - t0 < 15 and out["estimated"] is True and not out.get("error")
    r = rows(out)
    assert list(r)[:5] == ["torque", "servos", "electronics", "wiring", "geometry"]
    m0 = min(j["margin"] for j in base["sizing"]["joints"])
    assert out["torque"]["before_min"] == m0 and out["torque"]["after_min"] > 0
    assert f"{m0:g}× → {out['torque']['after_min']:g}× the torque it needs (the design asks for 2×)" in r["torque"]["text"]
    assert any(d.startswith("j2 shoulder: ") and "STS3215" in d and "XM430-W350" in d for d in r["torque"]["detail"])
    assert r["servos"]["text"].startswith("7× STS3215 → ") and "XL430-W250" in r["servos"]["text"]
    assert "Feetech SCS/STS serial bus → DYNAMIXEL Protocol 2.0 bus" in r["electronics"]["text"]
    assert "controller ESP32-S3-DevKitC-1 → ROBOTIS OpenRB-150" in r["electronics"]["text"]
    assert "Molex 5264" in r["wiring"]["text"] and "JST EH" in r["wiring"]["text"]
    assert "SCServo (Feetech) → Dynamixel2Arduino (ROBOTIS)" in r["wiring"]["text"]
    assert "servo bodies 45.2 × 24.7 × 35 mm (STS3215) → 46.5 × 28.5 × 34 mm" in r["geometry"]["text"]
    # the purchased parts, priced the way the electronics stage prices them: 7 STS3215 at $14 become ROBOTIS servos
    cost = out["cost"]
    servos = sum(ACTUATORS[k]["vendors"][0]["unit_price"] * n for k, n in out["servos"]["after"].items()) - 7 * 14.0
    assert cost["split"]["servos"] == pytest.approx(servos, abs=0.01) and cost["before"] == 300.0
    assert cost["after"] == pytest.approx(300.0 + cost["delta"], abs=0.01) and r["cost"]["tone"] == "bad"
    assert r["cost"]["text"].startswith(f"about +${cost['delta']:,.0f} per robot (servos +$") and "$300 → about $" in r["cost"]["text"]


def test_the_preview_flags_a_mixed_protocol_bus_before_it_builds_and_scopes_what_it_says(tmp_path):
    root = str(tmp_path / "egg-picker")
    real_base(root)
    out = impact.impact(root, "r001", {"arm.actuator_overrides": "j2=xm430_w350"})
    r = rows(out)
    assert r["issues"]["tone"] == "bad" and any("can't mix protocols" in i for i in r["issues"]["detail"])
    assert out["issues"]["added"] and not out["issues"]["cleared"]
    # a world-only change reruns the simulation and nothing else: one row
    assert [x["key"] for x in impact.impact(root, "r001", {"world.id": "desk"})["rows"]] == ["sim"]
    # a build-level change: the base build priced every level, so the parts made are estimated from it
    lvl = rows(impact.impact(root, "r001", {"build.config": "pilot"}))
    assert "prototype (mostly 3D-printed parts) → pilot (sheet metal, CNC and MJF parts)" == lvl["manufacturing"]["text"]
    assert "parts made +$240" in lvl["cost"]["text"] and "$300 → about $540" in lvl["cost"]["text"]
    # the custom PCB: the base build priced both electronics options
    pcb = rows(impact.impact(root, "r001", {"electronics.config": "custom_pcb"}))
    assert "off-the-shelf modules → a custom controller PCB" in pcb["electronics"]["text"]
    assert "about +$16 per robot (electronics +$16)" in pcb["cost"]["text"]
    # nothing to compare with, or nothing changes: no rows, never an exception
    assert impact.impact(root, "r009", {"arm.dof": 6})["error"] and impact.impact(root, "r001", {})["rows"] == []
    assert impact.impact(str(tmp_path / "nowhere"), "r001", {"arm.dof": 6})["rows"] == []


def _with_tooling(root, rev, per_config, tooling):
    p = os.path.join(root, rev, "results.json")
    with open(p, encoding="utf-8") as fh:
        res = json.load(fh)
    res["bom"]["totals"]["per_config"] = per_config
    res["manufacturing"] = {"configs": {k: {"tooling_cost": v} for k, v in tooling.items()}}
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(res, fh)


def test_mold_tooling_is_priced_apart_from_the_cost_per_robot(tmp_path):
    # a production switch at quantity 1 read "about +$77,000 per robot": the molds, paid once, spread over one robot
    per = {"prototype": 300.0, "pilot": 540.0, "production": 4771.0 + 72971.0}
    tooling = {"prototype": 0.0, "pilot": 0.0, "production": 72971.0}
    root = str(tmp_path / "egg-picker")
    real_base(root)
    _with_tooling(root, "r001", per, tooling)
    out = impact.impact(root, "r001", {"build.config": "production"})
    cost, text = out["cost"], rows(out)["cost"]["text"]
    assert cost["split"]["parts made"] == pytest.approx(4471.0) and cost["tooling"] == [0.0, 72971.0]
    assert cost["before"] == 300.0 and cost["after"] == pytest.approx(300.0 + cost["delta"]) and cost["delta"] < 10000
    assert "parts made +$4,471" in text and text.endswith("; plus $72,971 of mold tooling, paid once")
    assert "$77," not in text and rows(out)["cost"]["tone"] == "bad"
    # and back from production: the per-unit cost without the molds, and the molds it no longer needs
    root2 = str(tmp_path / "egg-picker-production")
    real_base(root2, changes={"build.config": "production"}, unit_cost=4771.0 + 72971.0)
    _with_tooling(root2, "r001", per, tooling)
    back = impact.impact(root2, "r001", {"build.config": "prototype"})
    text = rows(back)["cost"]["text"]
    assert back["cost"]["before"] == 4771.0 and back["cost"]["tooling"] == [72971.0, 0.0]
    assert "parts made -$4,471" in text and "$4,771 → about $" in text
    assert text.endswith("; and no mold tooling (it had $72,971, paid once)")
    # a build that keeps its molds says the per-unit cost is without them, and that they are paid once
    same = rows(impact.impact(root2, "r001", {"arm.actuator_family": "dynamixel"}))["cost"]["text"]
    assert "$4,771 → about $" in same and same.endswith("; plus the $72,971 of mold tooling, paid once")


def test_a_resize_that_keeps_the_grip_shows_no_grip_change(tmp_path):
    # the gripper stage reads sizing's servo rail, so it is downstream of sizing: a reach or wrist-camera edit marked it
    # affected, and the preview compared the built grip (from the grasp simulation) with New robot's closed form
    root = str(tmp_path / "egg-picker")
    _, res = real_base(root)
    res["gripper"]["force"]["target_force_n"] = 2.34          # the egg-picker's grasp simulation gives 2.34 N
    with open(os.path.join(root, "r001", "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    for change in ({"arm.reach_mm": 380.0}, {"electronics.extras": "wrist_camera"}):
        out = impact.impact(root, "r001", change)
        assert "grip" not in rows(out) and not out.get("error"), (change, rows(out).get("grip"))
    heavier = rows(impact.impact(root, "r001", {"object.mass_g": 300.0}))
    assert heavier["grip"]["text"].startswith("grip force target 2.34 → ")


def test_a_servo_rail_change_keeps_the_built_grip_in_the_preview(tmp_path, monkeypatch):
    # a 7.4 V rail on the egg-picker reruns the gripper stage, which keeps the grasp simulation's hold (2.34 N target);
    # the preview showed "grip force target 2.34 -> 2.17 N" and planned the electronics with New robot's closed form
    from morph.robot import electronics2

    root = str(tmp_path / "egg-picker")
    _, res = real_base(root)
    hi = res["gripper"]["force"]["window_n"][1]
    res["gripper"]["force"].update(hold_force_n=0.78, sim_hold_n=0.78, window_n=[1.17, hi], target_force_n=2.34)
    with open(os.path.join(root, "r001", "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    planned = []
    plan = electronics2.plan
    monkeypatch.setattr(electronics2, "plan", lambda d, sz, grip=None: planned.append(dict(grip or {})) or plan(d, sz, grip))
    out = impact.impact(root, "r001", {"electronics.servo_v": 7.4})
    assert not out.get("error") and "grip" not in rows(out), rows(out).get("grip")
    g = planned[-1]
    assert (g["target_force_n"], g["window_n"], g["hold_force_n"]) == (2.34, [1.17, hi], 0.78)
    assert g["servo_stall_force_n"] < 0.7 * res["gripper"]["force"]["servo_stall_force_n"]   # the rail moves the stall
    # so does a servo swap or another build level: the grasp is the same, the build keeps 2.34 N
    for change in ({"arm.actuator_family": "dynamixel"}, {"build.config": "production"}):
        out = impact.impact(root, "r001", change)
        assert not out.get("error") and "grip" not in rows(out), (change, rows(out).get("grip"))
        assert planned[-1]["target_force_n"] == 2.34
    # a different grasp is the preflight's: the pads or the object change what the grasp simulation finds
    assert "grip" in rows(impact.impact(root, "r001", {"finger.pad": "flat"}))


def test_a_finger_edit_previews_the_grip_its_gripper_stage_builds(tmp_path, monkeypatch):
    # a 6 mm pad depth on the egg-picker: the preview read "grip force target 2.34 -> 2.17 N" (New robot's closed form)
    # while the build's gripper stage, with its grasp simulation, keeps 2.34 N. The preview now finds the hold the same
    # way the stage does
    from morph.robot import electronics2, pipeline2

    root = str(tmp_path / "egg-picker")
    d, res = real_base(root)
    built = pipeline2.gripper_stage(d, res["sizing"])["force"]          # the base as its gripper stage builds it
    res["gripper"]["force"] = built
    with open(os.path.join(root, "r001", "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    planned = []
    plan = electronics2.plan
    monkeypatch.setattr(electronics2, "plan", lambda d, sz, grip=None: planned.append(dict(grip or {})) or plan(d, sz, grip))
    change = {"finger.pad_depth_mm": 6.0}
    stage = pipeline2.gripper_stage(d.with_changes(change), res["sizing"])["force"]
    out = impact.impact(root, "r001", change)
    assert not out.get("error") and stage["target_force_n"] == built["target_force_n"]
    assert "grip" not in rows(out), rows(out).get("grip")
    assert planned[-1]["target_force_n"] == stage["target_force_n"] and planned[-1]["hold_force_n"] == stage["hold_force_n"]
    # an edit that does move the grasp still shows the stage's new grip
    heavier = rows(impact.impact(root, "r001", {"object.mass_g": 120.0}))["grip"]["text"]
    stage = pipeline2.gripper_stage(d.with_changes({"object.mass_g": 120.0}), res["sizing"])["force"]
    assert heavier == f"grip force target {built['target_force_n']:g} → {stage['target_force_n']:g} N"


def test_teams_says_how_much_of_a_built_unit_cost_is_tooling(tmp_path):
    root = str(tmp_path / "egg-picker")
    real_base(root)
    real_base(root, rev="r002", changes={"build.config": "production"}, unit_cost=4771.0 + 72971.0)
    p = os.path.join(root, "r002", "results.json")
    with open(p, encoding="utf-8") as fh:
        res = json.load(fh)
    res["summary"]["tooling_cost"] = 72971.0
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    built = collab.built_summary(root, {"applied_rev": "r002", "base_rev": "r001"})
    assert built["unit_cost"] == [300.0, 77742.0] and built["tooling_cost"] == [None, 72971.0] and built["quantity"] == [1, 1]
    ck = collab.release_gate(root, "r002")["checklist"]
    assert (ck["unit_cost"], ck["tooling_cost"], ck["quantity"]) == (77742.0, 72971.0, 1)


def test_preview_and_change_requests_carry_the_numbers_and_readable_values(tmp_path):
    home = tmp_path / "projects"
    root = str(home / "egg-picker")
    ids, _ = make_team(str(home), TEAM)
    ms = members(str(home))
    real_base(root, by=ids["Jim"])
    pv = collab.preview(root, {"arm.actuator_family": "dynamixel"})
    assert pv["shown"] == [{"key": "arm.actuator_family", "label": "Arm · Actuators", "old": "Smart bus", "new": "Dynamixel"}]
    assert {x["key"] for x in pv["impact"]["rows"]} >= {"torque", "servos", "electronics", "wiring", "cost"}
    p = collab.create_proposal(root, ids["Ana"], None, {"arm.actuator_family": "dynamixel", "object.mass_g": 70}, members=ms)
    assert p["title"] == "Actuators to Dynamixel (+1 more)" and p["impact"]["rows"]
    q = collab.create_proposal(root, ids["Ana"], None, {"object.mass_g": 70}, members=ms)
    assert q["title"] == "Object mass to 70 g"
    assert collab.show_value("arm.payload_margin", 2.5) == "2.5×" and collab.show_value("electronics.extras", "wrist_camera,estop") \
        == "wrist camera + e-stop" and collab.show_value("electronics.extras", "") == "none"
    # the export of the project's history keeps the numbers and never mistakes them for people
    bundle = collab.export_bundle(root, ms)
    assert bundle["proposals"][0]["impact"]["cost"]["split"] and bundle["proposals"][0]["by"] == "Ana"


# ------------------------------------------------------------------ nobody approves their own work above prototype


def test_an_author_cant_approve_their_own_change_above_prototype_and_is_told_who_can(tmp_path):
    home = tmp_path / "projects"
    root = str(home / "egg-picker")
    ids, _ = make_team(str(home), TEAM)
    ms = members(str(home))
    build(root, "r001", changes={"build.config": "pilot"}, by=ids["Jim"])
    p = collab.create_proposal(root, ids["Ana"], "Dynamixel", {"arm.actuator_family": "dynamixel"}, members=ms)
    rv = collab.get_review(root, p["review"], ms)
    scope = collab.decision_scope(root, rv, ms[ids["Ana"]], ms)
    assert scope["covers"] == [] and scope["authored"] == ["electronics"]
    assert scope["why"] == "You made this change, so at pilot your approval doesn't count for Electronics: Jim (generalist) can approve it."
    with pytest.raises(collab.CollabError) as e:
        collab.decide(root, p["review"], ms[ids["Ana"]], "approve", members=ms)
    assert e.value.status == 403 and str(e.value) == scope["why"]
    with pytest.raises(collab.CollabError):                                          # nor block it herself
        collab.decide(root, p["review"], ms[ids["Ana"]], "request_changes", note="wait", members=ms)
    assert collab.decision_scope(root, rv, ms[ids["Raj"]], ms) == {"covers": ["manufacturing"], "authored": [], "why": None}
    assert collab.decision_scope(root, rv, ms[ids["Jim"]], ms)["covers"] == rv["disciplines"]
    vic = collab.decision_scope(root, rv, ms[ids["Vic"]], ms)
    assert vic["covers"] == [] and "Viewers can't approve on this team" in vic["why"]
    # the API says the same to each member (`mine`), and the inbox names the change request, not its id
    req = request(str(home), root, member=ms[ids["Ana"]], query={})
    mine = api_team.proposals_get(req)[1]["proposals"][0]["review"]["mine"]
    assert mine == scope
    status, out = api_team.reviews_decide(request(str(home), root, member=ms[ids["Raj"]], method="POST",
                                                  body={"review": p["review"], "decision": "approve"}))
    assert status == 200
    note = activity.inbox(str(home), ids["Ana"])["items"][0]
    assert note["kind"] == "decision" and note["text"] == "Raj approved change request #1: Dynamixel as Manufacturing"
    # at prototype the author may approve (contracts section 8)
    root2 = str(home / "sock-sorter")
    build(root2, "r001", by=ids["Jim"])
    p2 = collab.create_proposal(root2, ids["Ana"], "Dynamixel", {"arm.actuator_family": "dynamixel"}, members=ms)
    out = collab.decide(root2, p2["review"], ms[ids["Ana"]], "approve", members=ms)
    assert {x["discipline"]: x["state"] for x in out["per_discipline"]}["electronics"] == "approved"


def test_a_revision_review_judges_each_discipline_by_who_made_its_work(tmp_path):
    """Ana's change request, applied by Jim: r002 is Ana's work in every discipline, so Jim (generalist) approves it all
    at pilot; Ana's later custom-PCB edit leaves the simulation (reused) as Ana's r002 work and makes the rest hers too."""
    home = tmp_path / "projects"
    root = str(home / "egg-picker")
    ids, _ = make_team(str(home), TEAM)
    ms = members(str(home))
    build(root, "r001", changes={"build.config": "pilot"}, by=ids["Jim"])
    p = collab.create_proposal(root, ids["Ana"], "Dynamixel", {"arm.actuator_family": "dynamixel"}, members=ms)
    collab.decide(root, p["review"], ms[ids["Raj"]], "approve", members=ms)
    job = collab.apply_proposal(root, p["id"], ids["Jim"], members=ms)
    build(root, "r002", "r001", job["changes"], by=job["by"], proposed_by=job["proposed_by"])
    collab.proposal_done(root, p["id"], rev="r002")
    g = collab.release_gate(root, "r002", ms)
    assert g["missing"] == ["mechanical", "simulation", "electronics", "sourcing", "documentation"]   # Raj's counts already
    rv = collab.request_review(root, ids["Jim"], rev="r002", members=ms)
    assert collab.decision_scope(root, rv, ms[ids["Ana"]], ms)["authored"] == ["electronics"]
    out = collab.decide(root, rv["id"], ms[ids["Jim"]], "approve", members=ms)
    assert out["state"] == "approved" and collab.release_gate(root, "r002", ms)["ok"]
    rec = collab.revision_record(root, "r002", ms)
    assert rec["made_by"] == ["Ana"] and rec["applied_by"] == "Jim"
    build(root, "r003", "r002", {"electronics.config": "custom_pcb"}, by=ids["Ana"])
    g3 = collab.release_gate(root, "r003", ms)
    assert g3["missing"] == ["mechanical", "electronics", "manufacturing", "sourcing", "documentation"]
    assert [a["carried"] for d in g3["disciplines"] if d["discipline"] == "simulation" for a in d["approvers"]] == [True]
    assert collab.revision_record(root, "r003", ms)["applied_by"] is None       # Ana's own edit: nobody applied it for her


# ------------------------------------------------------------------ a removed member's open page ends


@pytest.fixture
def team_studio(tmp_path, monkeypatch):
    from morph import keys
    from test_studio_security import make_project
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home), team=True)
    ids, cookies = make_team(str(home), TEAM)
    httpd = start(st)
    port = httpd.server_address[1]
    jar = {n: f"{auth.cookie_name(port)}={c}" for n, c in cookies.items()}
    yield st, caller(httpd), ids, jar, port
    httpd.shutdown()


def stream(port, cookie):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("GET", "/api/events", headers={"Cookie": cookie})
    r = conn.getresponse()
    assert r.status == 200
    return conn, r


def events_until(r, kind, timeout=10.0) -> list:
    seen, t0 = [], time.time()
    while time.time() - t0 < timeout:
        line = r.fp.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            ev = json.loads(line[6:])
            seen.append(ev)
            if ev.get("type") == kind:
                return seen
    raise AssertionError(f"no {kind} event: {[e.get('type') for e in seen]}")


def test_revoking_a_member_ends_their_open_event_stream_and_their_page_says_why(team_studio):
    st, call, ids, jar, port = team_studio
    conn, r = stream(port, jar["Raj"])
    jconn, jr = stream(port, jar["Jim"])
    assert call("POST", "/api/team/revoke", {"member": ids["Raj"]}, headers={"Cookie": jar["Jim"]})[0] == 200
    seen = events_until(r, "signed_out")
    assert seen[-1] == {"type": "signed_out", "project": "egg-picker", "reason": "removed"}
    assert r.fp.read().strip() == b""                                                      # and the stream is closed
    conn.close()
    ev = events_until(jr, "member")[-1]                                                    # everyone else's tabs hear it
    assert ev["action"] == "revoked" and ev["member"]["id"] == ids["Raj"] and ev["by"] == ids["Jim"]
    jconn.close()
    s, page, _ = call("GET", "/", headers={"Cookie": jar["Raj"]})
    assert s == 401 and "You're no longer on R Cubed" in page and "Jim removed Raj from this Studio" in page
    assert call("GET", "/api/state", headers={"Cookie": jar["Raj"]})[0] == 401
    # someone else's cookie with Raj's id but a forged signature learns nothing
    forged = jar["Raj"].rsplit(".", 1)[0] + "." + "A" * 43
    s, page, _ = call("GET", "/", headers={"Cookie": forged})
    assert s == 401 and "no longer" not in page and "This Studio is shared with a team" in page
    # "sign out everywhere" ends Ana's open streams too, with its own words
    conn, r = stream(port, jar["Ana"])
    auth.sign_out_everywhere(ids["Ana"], home=st.home)
    assert events_until(r, "signed_out")[-1]["reason"] == "signed_out"
    conn.close()
    s, page, _ = call("GET", "/", headers={"Cookie": jar["Ana"]})
    assert s == 401 and "Signed out of R Cubed" in page


def test_a_viewer_is_told_what_to_do_instead_and_the_join_page_explains_the_invite(team_studio):
    st, call, ids, jar, port = team_studio
    s, out, _ = call("POST", "/api/params", {"changes": {"arm.dof": 6}}, headers={"Cookie": jar["Vic"]})
    assert s == 403 and out["error"] == ("Viewers can't change the design: propose the change under Changes in the right panel, "
                                         "or ask an owner to make you an editor.")
    assert call("POST", "/api/reset", {}, headers={"Cookie": jar["Ana"]})[1]["error"] == "Only the team's owners can do that."
    s, inv, _ = call("POST", "/api/team/invites", {"role": "editor", "disciplines": ["manufacturing", "generalist"], "name_hint": "Mo"},
                     headers={"Cookie": jar["Jim"]})
    s, page, _ = call("GET", f"/?invite={inv['code']}")
    text = " ".join(page.replace("<", " <").split())
    assert "You'll edit and build the robot" in text and "You review changes to:" in text and "Manufacturing" in text
    assert "As a generalist you also review every stage that has no specialist on the team." in text


def test_team_init_says_what_to_do_next_before_the_link(tmp_path):
    lines = []
    team_cli.team_init("Jim", "R Cubed", home=str(tmp_path / "projects"), out=lines.append)
    assert lines[1].startswith("Next, start the Studio in team mode: morph studio --team")
    assert "already running at http://127.0.0.1:8765" in lines[3] and "?link=ml_" in lines[4]
