"""Teams collaboration: comments that carry forward, change requests with attribution, reviews tied to the exact
revision, the release gate by build level, branches, releases and gated order downloads."""

import json
import os

import pytest

from morph.studio.teams import auth, collab
from morph.studio.teams.disciplines import DISCIPLINES
from test_team_util import build, make_team, members

STS = collab.bom_key("actuator", "Feetech STS3215 serial bus servo 12 V")
DXL = collab.bom_key("actuator", "Dynamixel XL430-W250-T servo")


@pytest.fixture(autouse=True)
def _fresh():
    auth.THROTTLE.clear()


@pytest.fixture
def team(tmp_path):
    home = tmp_path / "projects"
    root = home / "egg-picker"
    ids, cookies = make_team(home)
    return str(home), str(root), ids, members(home)


# ------------------------------------------------------------------ comments


def test_comments_carry_forward_while_their_anchor_exists(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"], files=("cad/robot.glb",))
    build(root, "r002", "r001", {"fab.process": "cnc_router"}, by=ids["Raj"], files=("cad/robot.glb",))
    build(root, "r003", "r002", {"arm.actuator_family": "dynamixel"}, by=ids["Mo"])
    add = lambda kind, key, body: collab.add_comment(root, ids["Ana"], body, {"rev": "r001", "kind": kind, "key": key}, members=ms)
    c_stage, c_param = add("stage", "wiring", "Check the bus pull-up"), add("param", "arm.reach_mm", "Reach is tight")
    c_bom, c_file = add("bom", STS, "Is the 12 V version in stock?"), add("file", "cad/robot.glb", "Base looks thin")
    c_rev = collab.add_comment(root, ids["Jim"], "Nice first cut", {"rev": "r001"}, members=ms)
    reply = collab.add_comment(root, ids["Sue"], "Seeed has 40", parent=c_bom["id"], members=ms)
    assert c_bom["anchor"]["label"] == "Feetech STS3215 serial bus servo 12 V" and reply["anchor"] == c_bom["anchor"]

    on1 = {c["id"]: c for c in collab.comments(root, "r001")}
    assert set(on1) == {c_stage["id"], c_param["id"], c_bom["id"], c_file["id"], c_rev["id"], reply["id"]}
    assert not any(c["carried"] for c in on1.values())

    on2 = {c["id"]: c for c in collab.comments(root, "r002")}  # fab change: wiring reused, BOM rows unchanged
    assert c_rev["id"] not in on2                               # revision comments stay on their revision
    assert {c_stage["id"], c_param["id"], c_bom["id"], c_file["id"], reply["id"]} <= set(on2)
    assert all(on2[i]["carried"] for i in on2) and not on2[c_stage["id"]]["outdated"] and not on2[c_param["id"]]["outdated"]
    assert on2[c_file["id"]]["outdated"]                        # the file's content differs in r002

    on3 = {c["id"]: c for c in collab.comments(root, "r003")}   # servo swap: the STS3215 line is gone
    assert c_bom["id"] not in on3 and reply["id"] not in on3
    assert c_file["id"] not in on3                              # r003 has no robot.glb
    assert on3[c_stage["id"]]["outdated"] and on3[c_stage["id"]]["carried"]   # wiring was recomputed
    assert not on3[c_param["id"]]["outdated"]
    assert collab.comments(root, "r003", kind="bom") == []
    dxl = collab.add_comment(root, ids["Sue"], "Dynamixel lead time is 3 weeks", {"rev": "r003", "kind": "bom", "key": DXL}, members=ms)
    assert [c["id"] for c in collab.comments(root, "r003", kind="bom")] == [dxl["id"]]


def test_comments_follow_lineage_and_resolve_by_thread(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    build(root, "r002", "r001", {"arm.dof": 6}, by=ids["Mo"])
    build(root, "r003", "r001", {"arm.dof": 4}, by=ids["Mo"])  # a sibling line from r001
    c = collab.add_comment(root, ids["Ana"], "6 joints need a bigger PSU", {"rev": "r002", "kind": "stage", "key": "electronics"})
    assert [x["id"] for x in collab.comments(root, "r002")] == [c["id"]]
    assert collab.comments(root, "r003") == []                 # not an ancestor: never carried sideways
    r = collab.add_comment(root, ids["Jim"], "Agreed", parent=c["id"])
    out = collab.resolve_comment(root, r["id"], ids["Jim"])   # resolving a reply resolves its thread
    assert out["id"] == c["id"] and out["resolved"] and out["resolved_by"] == ids["Jim"]
    assert collab.comments(root, "r002", include_resolved=False) == []
    collab.resolve_comment(root, c["id"], ids["Ana"], resolved=False)
    assert len(collab.comments(root, "r002", include_resolved=False)) == 2


def test_comment_input_is_validated(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    for body in ("", "   ", "x" * 4001, "my key sk-ant-api03-abcdefghijkl", 42):
        with pytest.raises(collab.CollabError) as e:
            collab.add_comment(root, ids["Ana"], body, {"rev": "r001"})
        assert e.value.status == 400
    for anchor, status in (({"rev": "r009"}, 404), ({"rev": "r001", "kind": "bom", "key": "0000000000"}, 400),
                           ({"rev": "r001", "kind": "file", "key": "../../etc/passwd"}, 400), ({"rev": "r001", "kind": "spice"}, 400),
                           ({"rev": "r001", "kind": "param", "key": "arm.colour"}, 400), ({"rev": "r001", "kind": "file", "key": ".team/x"}, 400)):
        with pytest.raises(collab.CollabError) as e:
            collab.add_comment(root, ids["Ana"], "hello", anchor)
        assert e.value.status == status, anchor
    c = collab.add_comment(root, ids["Ana"], "ping @raj and @Jim, cc @nobody", {"rev": "r001"}, members=ms)
    assert set(c["mentions"]) == {ids["Raj"], ids["Jim"]}


# ------------------------------------------------------------------ change requests


def test_a_viewers_change_request_is_applied_with_attribution(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    p = collab.create_proposal(root, ids["Vic"], "Swap to Dynamixel", {"arm.actuator_family": "dynamixel"}, members=ms)
    assert p["n"] == 1 and p["base_rev"] == "r001" and p["status"] == "open" and p["diff"] == {"arm.actuator_family": ["smart_bus", "dynamixel"]}
    assert set(p["disciplines"]) >= {"mechanical", "electronics", "manufacturing", "sourcing"}
    rv = collab.get_review(root, p["review"], ms)
    assert rv["target"] == {"kind": "proposal", "proposal": p["id"], "rev": None} and rv["authors"] == [ids["Vic"]]
    asked = {r["discipline"]: r["members"] for r in rv["requested_from"]}
    assert asked["electronics"] == [ids["Ana"]] and asked["manufacturing"] == [ids["Raj"]] and asked["sourcing"] == [ids["Sue"]]
    job = collab.apply_proposal(root, p["id"], ids["Ana"], members=ms)
    assert job["by"] == ids["Ana"] and job["proposed_by"] == ids["Vic"] and job["changes"] == {"arm.actuator_family": "dynamixel"}
    assert job["base_rev"] == "r001" and job["conflicts"] == [] and "from Vic" in job["text"] and job["proposal"] == p["id"]
    assert collab.get_proposal(root, p["id"])["status"] == "applied"
    with pytest.raises(collab.CollabError) as e:
        collab.apply_proposal(root, p["id"], ids["Ana"])
    assert e.value.status == 409
    collab.proposal_done(root, p["id"], error="sizing failed")            # a failed build reopens it
    assert collab.get_proposal(root, p["id"])["status"] == "open"
    collab.apply_proposal(root, p["id"], ids["Raj"])
    build(root, "r002", "r001", {"arm.actuator_family": "dynamixel"}, by=ids["Raj"], proposed_by=ids["Vic"])
    done = collab.proposal_done(root, p["id"], rev="r002")
    assert done["status"] == "applied" and done["applied_rev"] == "r002" and done["applied_by"] == ids["Raj"]
    assert collab.authors(root, "r002") == {ids["Vic"]}          # the author, not whoever applied it
    assert collab.rev_by(root, "r002")["by"] == ids["Raj"] and collab.rev_by(root, "r002")["proposed_by"] == ids["Vic"]
    with pytest.raises(collab.CollabError):
        collab.close_proposal(root, p["id"], ids["Raj"])


def test_change_requests_report_conflicts_and_validate_changes(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    p = collab.create_proposal(root, ids["Ana"], None, {"build.config": "pilot", "electronics.mcu": "rp2040_pico"}, members=ms)
    assert p["title"].startswith("Build to Pilot") and "(+1 more)" in p["title"]    # option names, as the Parameters panel
    build(root, "r002", "r001", {"build.config": "production"}, by=ids["Raj"])   # main moved underneath it
    assert collab.head_conflicts(root, collab.get_proposal(root, p["id"])) == ["build.config"]
    assert collab.apply_proposal(root, p["id"], ids["Jim"])["conflicts"] == ["build.config"]
    doc = collab.create_proposal(root, ids["Vic"], "Add the torque spec to step 4", stages=["manual"], members=ms)
    assert doc["disciplines"] == ["documentation"] and doc["changes"] == {}
    with pytest.raises(collab.CollabError) as e:
        collab.apply_proposal(root, doc["id"], ids["Jim"])
    assert e.value.status == 409
    assert collab.close_proposal(root, doc["id"], ids["Jim"], "done by hand")["status"] == "closed"
    for bad in ({"arm.reach_mm": 900}, {"arm.colour": "red"}, {"arm.dof": 7}, {"arm.reach_mm": float("nan")},
                {"finger.pad": ["v"]}, {"arm.source": "../../etc"}, {"build.quantity": 2.5}, {"electronics.servo": True}):
        with pytest.raises(collab.CollabError) as e:
            collab.create_proposal(root, ids["Ana"], "x", bad)
        assert e.value.status == 400, bad
    with pytest.raises(collab.CollabError):
        collab.create_proposal(root, ids["Ana"], "nothing", {"arm.dof": 5})       # already 5: a no-op
    assert collab.validate_changes({"arm.dof": "6", "arm.reach_mm": "350", "electronics.servo": "mg996r"}) == \
        {"arm.dof": 6, "arm.reach_mm": 350.0, "electronics.servo": "mg996r"}


# ------------------------------------------------------------------ the release gate (pure)

TEAM = {
    "jim": {"id": "jim", "name": "Jim", "role": "owner", "disciplines": ["generalist"]},
    "ana": {"id": "ana", "name": "Ana", "role": "editor", "disciplines": ["electronics"]},
    "raj": {"id": "raj", "name": "Raj", "role": "editor", "disciplines": ["manufacturing"]},
    "mo": {"id": "mo", "name": "Mo", "role": "editor", "disciplines": ["mechanical"]},
    "sue": {"id": "sue", "name": "Sue", "role": "editor", "disciplines": ["sourcing"]},
    "dee": {"id": "dee", "name": "Dee", "role": "editor", "disciplines": ["documentation"]},
    "sam": {"id": "sam", "name": "Sam", "role": "editor", "disciplines": ["simulation"]},
    "vic": {"id": "vic", "name": "Vic", "role": "viewer", "disciplines": ["documentation"]},
}
SPEC = {"mechanical": "mo", "simulation": "sam", "electronics": "ana", "manufacturing": "raj", "sourcing": "sue", "documentation": "dee"}


def ap(by, *ds, decision="approve", authors=None):
    d = {"by": by, "decision": decision, "disciplines": list(ds)}
    if authors is not None:
        d["authors"] = list(authors)
    return d


ALL_SPECIALISTS = [ap(m, d) for d, m in SPEC.items()]

GATE_CASES = [
    # (level, decisions, revision authors, policy, ok, short text starts with)
    ("prototype", [], {"ana"}, {}, False, "needs 1 approval: prototype"),
    ("prototype", [ap("ana", "electronics")], {"ana"}, {}, True, "ready to release: prototype"),          # the author may approve
    ("prototype", [{**ap("raj", "manufacturing"), "carried": True}], {"ana"}, {}, False, "needs 1 approval: prototype"),
    ("prototype", [ap("ana", "electronics"), ap("raj", "manufacturing", decision="request_changes")], {"ana"}, {}, False,
     "changes requested: Manufacturing (Raj)"),
    ("prototype", [ap("vic", "documentation")], {"ana"}, {}, False, "needs 1 approval"),                     # viewers can't
    ("prototype", [ap("vic", "documentation")], {"ana"}, {"viewers_can_approve": True}, True, "ready"),
    ("pilot", ALL_SPECIALISTS, {"jim"}, {}, True, "ready to release: pilot"),
    ("pilot", ALL_SPECIALISTS[:4], {"jim"}, {}, False, "needs 2 approvals: pilot"),
    ("pilot", ALL_SPECIALISTS, {"ana"}, {}, False, "needs 1 approval: pilot"),                               # the author never counts
    ("pilot", [ap("jim", *DISCIPLINES)], {"ana"}, {}, True, "ready to release: pilot"),                     # a generalist covers all
    ("pilot", [ap("jim", *DISCIPLINES)], {"jim"}, {}, False, "needs 6 approvals: pilot"),                   # ...but not their own work
    ("pilot", [ap("jim", *DISCIPLINES)], {"ana"}, {"specialist_required": {"pilot": ["electronics"]}}, False, "needs 1 approval"),
    ("pilot", [ap("jim", *DISCIPLINES), ap("ana", "electronics")], {"mo"}, {"specialist_required": {"pilot": ["electronics"]}}, True, "ready"),
    ("pilot", ALL_SPECIALISTS + [ap("ana", "electronics", decision="request_changes")], {"jim"}, {}, False, "changes requested: Electronics"),
    ("pilot", [ap("ana", "electronics", decision="request_changes")] + ALL_SPECIALISTS, {"jim"}, {}, True, "ready"),  # latest wins
    ("production", ALL_SPECIALISTS, {"vic"}, {}, False, "needs an owner's approval: production"),
    ("production", ALL_SPECIALISTS + [ap("jim", "documentation")], {"vic"}, {}, True, "ready to release: production"),
    ("production", [ap("jim", *DISCIPLINES)], {"ana"}, {}, False, "needs 1 more approver who didn't make the change"),
    ("production", [ap("jim", *DISCIPLINES), ap("raj", "manufacturing")], {"ana"}, {}, True, "ready to release: production"),
    ("production", ALL_SPECIALISTS + [ap("jim", "documentation")], {"jim"}, {}, False, "needs an owner's approval"),  # owner wrote it
    ("production", [ap("jim", *DISCIPLINES), ap("raj", "manufacturing")], {"ana"},
     {"specialist_required": {"production": ["electronics"]}}, False, "needs 1 approval: production"),
]


@pytest.mark.parametrize("level,decs,authors,policy,ok,short", GATE_CASES)
def test_release_gate_truth_table(level, decs, authors, policy, ok, short):
    g = collab.can_release(level, decs, TEAM, authors=authors, policy=policy)
    assert g["ok"] is ok and g["short"].startswith(short), g["short"]
    assert g["level"] == level and len(g["disciplines"]) == 6


def test_gate_rows_name_who_is_needed_and_ignore_revoked_members():
    g = collab.can_release("pilot", ALL_SPECIALISTS[1:], TEAM, authors={"jim"})
    mech = g["disciplines"][0]
    assert mech["discipline"] == "mechanical" and mech["state"] == "missing"
    assert {n["name"] for n in mech["needs"]} == {"Mo"}           # Jim (generalist) wrote it, so only Mo can approve
    assert "Mechanical" in g["text"]
    gone = {**TEAM, "mo": {**TEAM["mo"], "revoked": True}}
    assert not collab.can_release("pilot", ALL_SPECIALISTS, gone, authors={"jim"})["ok"]
    per_work = collab.can_release("pilot", ALL_SPECIALISTS, TEAM, authors={"jim"}, work_authors={"electronics": ["ana"]})
    assert per_work["missing"] == ["electronics"]                 # Ana made the electronics work she approved
    with pytest.raises(ValueError):
        collab.can_release("beta", [], TEAM)


# ------------------------------------------------------------------ reviews against stored revisions


def test_decisions_are_tied_to_the_exact_revision_and_its_stage_artifacts(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    rv = collab.request_review(root, ids["Jim"], rev="r001", members=ms)
    assert rv["disciplines"] == list(DISCIPLINES)
    with pytest.raises(collab.CollabError) as e:
        collab.decide(root, rv["id"], ms[ids["Vic"]], "approve", members=ms)          # viewers can't approve by default
    assert e.value.status == 403
    with pytest.raises(collab.CollabError) as e:
        collab.decide(root, rv["id"], ms[ids["Raj"]], "approve", disciplines=["electronics"], members=ms)
    assert e.value.status == 403
    out = collab.decide(root, rv["id"], ms[ids["Ana"]], "approve", "LGTM", members=ms)
    dec = out["decisions"][-1]
    assert dec["disciplines"] == ["electronics"] and dec["rev"] == "r001"
    assert set(dec["stage_hashes"]) == {"electronics", "wiring"} and all(dec["stage_hashes"].values())
    assert {p["discipline"]: p["state"] for p in out["per_discipline"]}["electronics"] == "approved"
    with pytest.raises(collab.CollabError):
        collab.decide(root, rv["id"], ms[ids["Ana"]], "maybe", members=ms)
    # the revision name is reused by a different design (a demo reset): the review can't be decided any more
    build(root, "r001", by=ids["Jim"], changes={"arm.dof": 4})
    with pytest.raises(collab.CollabError) as e:
        collab.decide(root, rv["id"], ms[ids["Raj"]], "approve", members=ms)
    assert e.value.status == 409
    assert collab.current_decisions(root, "r001") == []


def test_flagship_servo_swap_is_routed_approved_released_and_goes_stale_only_where_it_changed(team):
    """Contracts section 8: propose a servo substitution, preview it, route it, approve it against the exact revision,
    release it; a later edit makes only the affected disciplines' approvals stale."""
    home, root, ids, ms = team
    build(root, "r001", changes={"build.config": "pilot"}, by=ids["Jim"], files=("mfg/mfg_pilot.zip", "mfg/controller_pcba/gerbers.zip"))
    p = collab.create_proposal(root, ids["Mo"], "Swap to Dynamixel XL430", {"arm.actuator_family": "dynamixel"}, members=ms)
    pv = collab.preview(root, {"arm.actuator_family": "dynamixel"})
    assert {"Sizing", "Robot CAD", "Schematic · PCB · FW", "BOM + sourcing"} <= set(pv["stage_titles"]) and pv["level"] == "pilot"
    rv = collab.get_review(root, p["review"], ms)
    asked = {r["discipline"]: (r["via"], r["members"]) for r in rv["requested_from"]}
    assert asked["electronics"] == ("specialist", [ids["Ana"]]) and asked["manufacturing"] == ("specialist", [ids["Raj"]])
    assert asked["sourcing"] == ("specialist", [ids["Sue"]]) and asked["mechanical"] == ("generalist", [ids["Jim"]])
    for who in ("Ana", "Raj", "Sue", "Jim"):
        collab.decide(root, p["review"], ms[ids[who]], "approve", members=ms)
    assert collab.get_review(root, p["review"], ms)["state"] == "approved"
    job = collab.apply_proposal(root, p["id"], ids["Mo"], members=ms)
    build(root, "r002", "r001", job["changes"], by=job["by"], proposed_by=job["proposed_by"],
          files=("mfg/mfg_pilot.zip", "mfg/controller_pcba/gerbers.zip"))
    collab.proposal_done(root, p["id"], rev="r002")
    g = collab.release_gate(root, "r002", ms)
    assert g["ok"] and g["level"] == "pilot" and g["missing"] == [] and g["authors"] == [ids["Mo"]], g["text"]
    assert {a for row in g["disciplines"] for a in [x["id"] for x in row["approvers"]]} == {ids["Ana"], ids["Raj"], ids["Sue"], ids["Jim"]}
    assert not collab.download(root, "r002", "mfg/mfg_pilot.zip", ms[ids["Vic"]])["allowed"]
    rel = collab.create_release(root, "Pilot v1", "r002", ids["Jim"], ms, home=home)
    assert rel["level"] == "pilot" and rel["design_sha256"] == collab.rev_info(root, "r002")["design_sha256"]
    assert {a["discipline"] for a in rel["approvals"]} == set(DISCIPLINES) and collab.pinned(root) == {"r002"}
    assert collab.download(root, "r002", "mfg/mfg_pilot.zip", ms[ids["Vic"]]) == {"allowed": True, "draft": False, "reason": ""}

    # a later edit by Raj recomputes only manufacturing, BOM and the manual
    build(root, "r003", "r002", {"fab.process": "cnc_router"}, by=ids["Raj"])
    g3 = collab.release_gate(root, "r003", ms)
    assert not g3["ok"] and g3["missing"] == ["manufacturing", "sourcing", "documentation"]
    assert g3["short"] == "needs 3 approvals: pilot"
    carried = [d for d in collab.current_decisions(root, "r003") if d["carried"]]
    assert {d["disciplines"][0] for d in carried} == {"mechanical", "simulation", "electronics"}
    rv3 = collab.request_review(root, ids["Raj"], rev="r003", members=ms)
    assert rv3["disciplines"] == ["manufacturing", "sourcing", "documentation"]
    assert {r["discipline"]: r["via"] for r in rv3["requested_from"]} == {"manufacturing": "generalist", "sourcing": "specialist",
                                                                           "documentation": "generalist"}
    with pytest.raises(collab.CollabError) as e:                                     # his own work: refused, and why
        collab.decide(root, rv3["id"], ms[ids["Raj"]], "approve", members=ms)
    assert e.value.status == 403 and "You made this change" in str(e.value) and "Jim (generalist)" in str(e.value)
    assert collab.decision_scope(root, rv3, ms[ids["Raj"]], ms)["authored"] == ["manufacturing"]
    assert collab.release_gate(root, "r003", ms)["missing"] == ["manufacturing", "sourcing", "documentation"]
    collab.decide(root, rv3["id"], ms[ids["Sue"]], "approve", members=ms)
    collab.decide(root, rv3["id"], ms[ids["Jim"]], "approve", disciplines=["manufacturing", "documentation"], members=ms)
    assert collab.release_gate(root, "r003", ms)["ok"]
    with pytest.raises(collab.CollabError) as e:
        collab.create_release(root, "pilot V1", "r003", ids["Jim"], ms)               # tags are unique (case-insensitive)
    assert e.value.status == 409


def test_a_release_refuses_until_the_gate_passes(team):
    home, root, ids, ms = team
    build(root, "r001", changes={"build.config": "production"}, by=ids["Ana"])
    with pytest.raises(collab.CollabError) as e:
        collab.create_release(root, "Production 1", "r001", ids["Jim"], ms)
    assert e.value.status == 409 and e.value.gate["level"] == "production" and not e.value.gate["ok"]
    for bad in ("", "x" * 41, "v1; rm -rf", None):
        with pytest.raises(collab.CollabError):
            collab.create_release(root, bad, "r001", ids["Jim"], ms)


# ------------------------------------------------------------------ branches


def test_branches_compare_and_merge_as_a_change_request(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    b = collab.create_branch(root, "dxl-trial", ids["Mo"])
    assert b["base_rev"] == "r001" and b["head_rev"] == "r001"
    build(root, "r002", "r001", {"arm.actuator_family": "dynamixel", "arm.dof": 4}, by=ids["Mo"])
    collab.advance_branch(root, "dxl-trial", "r002", ids["Mo"])
    build(root, "r003", "r001", {"arm.dof": 6, "fab.process": "cnc_router"}, by=ids["Raj"])   # main moves on
    assert collab.head(root) == "r003" and collab.head(root, "dxl-trial") == "r002"
    assert [x["name"] for x in collab.branches(root)] == ["main", "dxl-trial"]
    cmp = collab.compare(root, "main", "dxl-trial")
    assert cmp["a"]["rev"] == "r003" and cmp["b"]["rev"] == "r002" and cmp["merge_base"] == "r001"
    assert cmp["changes"]["arm.dof"] == [6, 4] and cmp["changes"]["fab.process"] == ["cnc_router", "auto"]
    p = collab.merge_as_proposal(root, "dxl-trial", ids["Mo"], members=ms)
    assert p["base_rev"] == "r003" and p["branch"] == "main"
    assert p["changes"] == {"arm.actuator_family": "dynamixel", "arm.dof": 4}
    assert p["source"]["conflicts"] == [{"key": "arm.dof", "base": 5, "main": 6, "branch": 4}] and "Conflicts with main: arm.dof" in p["note"]
    with pytest.raises(collab.CollabError) as e:
        collab.advance_branch(root, "dxl-trial", "r003")              # r003 doesn't build on the branch head
    assert e.value.status == 409
    for name, status in (("main", 400), ("Bad Name", 400), ("dxl-trial", 409)):
        with pytest.raises(collab.CollabError) as e:
            collab.create_branch(root, name, ids["Mo"])
        assert e.value.status == status
    with pytest.raises(collab.CollabError):
        collab.merge_as_proposal(root, "nope", ids["Mo"])


# ------------------------------------------------------------------ downloads and export


def test_order_packages_unlock_on_release_and_drafts_follow_the_policy(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    for rel in ("mfg/mfg_prototype.zip", "mfg/controller_pcba/gerbers.zip", "mfg/controller_pcba/jlc_bom.csv",
                "electronics/board/gerbers/top.gtl", "electronics/board/jlc_cpl.csv"):
        assert collab.order_package(rel), rel
    for rel in ("cad/robot.glb", "manual/manual.html", "electronics/schematic.svg", "mfg/README.txt"):
        assert not collab.order_package(rel), rel
    z = "mfg/mfg_prototype.zip"
    assert collab.download(root, "r001", "cad/robot.glb", ms[ids["Vic"]])["allowed"]
    assert collab.download(root, "r001", z, ms[ids["Jim"]])["draft"] is True
    assert collab.download(root, "r001", z, ms[ids["Ana"]], {"unreleased_downloads": "warn"})["allowed"]
    assert not collab.download(root, "r001", z, ms[ids["Ana"]], {"unreleased_downloads": "owner"})["allowed"]
    assert not collab.download(root, "r001", z, ms[ids["Vic"]], {"unreleased_downloads": "warn"})["allowed"]


def test_export_bundle_names_people_instead_of_ids(team):
    home, root, ids, ms = team
    build(root, "r001", by=ids["Jim"])
    collab.add_comment(root, ids["Ana"], "Looks good", {"rev": "r001"})
    collab.create_proposal(root, ids["Raj"], "CNC", {"fab.process": "cnc_router"}, members=ms)
    blob = json.dumps(collab.export_bundle(root, ms))
    assert '"by": "Ana"' in blob and '"by": "Raj"' in blob
    assert not any(mid in blob for mid in ids.values())


def test_revisions_sort_numerically_and_skip_unfinished_builds(team):
    home, root, ids, ms = team
    for i in (1, 2, 10, 999, 1000):
        build(root, f"r{i:03d}", by=ids["Jim"])
    os.makedirs(os.path.join(root, "r1001"))
    with open(os.path.join(root, "r1001", "design.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")  # results.json not written yet: still building
    assert collab.revs(root) == ["r001", "r002", "r010", "r999", "r1000"] and collab.head(root) == "r1000"
