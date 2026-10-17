"""Stage owners by discipline (CODEOWNERS): which disciplines a change touches, who is asked, generalist fallback."""

from morph.robot.pipeline2 import ORDER, dirty_nodes
from morph.studio.teams import disciplines as D

M = {
    "jim": {"id": "jim", "name": "Jim", "role": "owner", "disciplines": ["generalist"]},
    "ana": {"id": "ana", "name": "Ana", "role": "editor", "disciplines": ["electronics"]},
    "raj": {"id": "raj", "name": "Raj", "role": "editor", "disciplines": ["manufacturing"]},
    "mo": {"id": "mo", "name": "Mo", "role": "editor", "disciplines": ["mechanical"]},
    "sue": {"id": "sue", "name": "Sue", "role": "editor", "disciplines": ["sourcing"]},
    "vic": {"id": "vic", "name": "Vic", "role": "viewer", "disciplines": ["documentation"]},
}


def test_every_stage_has_exactly_one_owning_discipline():
    assert sorted(D.STAGE_DISCIPLINE) == sorted(ORDER)
    assert D.STAGES["mechanical"] == ("sizing", "gripper", "cad") and D.STAGES["electronics"] == ("electronics", "wiring")
    assert D.STAGES["sourcing"] == ("bom",) and D.STAGES["documentation"] == ("manual",) and D.STAGES["simulation"] == ("sim",)


def test_a_servo_swap_routes_to_mechanical_electronics_manufacturing_and_sourcing():
    for change in ({"electronics.servo": "mg996r"}, {"arm.actuator_family": "dynamixel"}, {"arm.actuator_overrides": "j2=xm430_w350"}):
        ds = D.affected_disciplines(change)
        assert {"mechanical", "electronics", "manufacturing", "sourcing"} <= set(ds)
        # exactly the pipeline's own dirty set: sizing feeds the simulation and every stage feeds the manual
        assert ds == D.disciplines_for_stages(dirty_nodes(change.keys())) == list(D.DISCIPLINES)
        r = D.routing(change, author="mo", members=M)
        chips = {x["discipline"]: [m["name"] for m in x["members"]] for x in r["routes"]}
        assert chips["electronics"] == ["Ana"] and chips["manufacturing"] == ["Raj"] and chips["sourcing"] == ["Sue"]
        assert chips["mechanical"] == ["Jim"]  # Mo wrote it, so the generalist covers mechanical
        assert "mo" not in r["reviewers"]


def test_a_pure_manual_change_routes_to_documentation_only():
    assert D.affected_stages(stages=["manual"]) == ["manual"]
    assert D.affected_disciplines(stages=["manual"]) == ["documentation"]
    assert D.affected_disciplines(["manual"]) == ["documentation"]   # a stage name as a change key
    r = D.routing(stages=["manual"], author="ana", members=M)
    assert [x["discipline"] for x in r["routes"]] == ["documentation"]
    assert r["routes"][0]["members"][0]["name"] == "Vic" and r["reviewers"] == ["vic"]


def test_downstream_stages_follow_the_pipeline_graph():
    assert D.affected_stages({"fab.process": "cnc_router"}) == ["manufacturing", "bom", "manual"]
    assert D.affected_disciplines({"fab.process": "cnc_router"}) == ["manufacturing", "sourcing", "documentation"]
    assert D.affected_stages(stages=["wiring"]) == ["wiring", "manufacturing", "bom", "manual"]
    assert D.affected_disciplines({"electronics.config": "custom_pcb"}) == ["mechanical", "electronics", "manufacturing",
                                                                            "sourcing", "documentation"]
    assert D.affected_disciplines({}) == [] and D.affected_disciplines(None) == []


def test_the_author_is_never_asked_and_generalists_cover_disciplines_without_another_specialist():
    r = D.routing({"electronics.config": "custom_pcb"}, author="ana", members=M)
    by = {x["discipline"]: x for x in r["routes"]}
    assert by["electronics"]["via"] == "generalist" and [m["id"] for m in by["electronics"]["members"]] == ["jim"]
    assert by["documentation"]["via"] == "specialist"
    assert "ana" not in r["reviewers"] and set(r["reviewers"]) == {"jim", "mo", "raj", "sue", "vic"}
    solo = {"jim": {**M["jim"], "disciplines": ["mechanical"]}, "ana": M["ana"]}
    r = D.routing({"electronics.mcu": "rp2040_pico"}, author="ana", members=solo)
    by = {x["discipline"]: x for x in r["routes"]}
    assert by["electronics"]["via"] == "owner"           # no other specialist, no generalist: the owner
    assert by["mechanical"]["via"] == "specialist"
    assert D.reviewers_for({"electronics.mcu": "rp2040_pico"}, "jim", {"jim": solo["jim"]}) == []


def test_chip_text_reads_like_the_share_dialog():
    r = D.routing({"electronics.config": "custom_pcb"}, author="mo", members=M)
    assert "Electronics: Ana" in r["text"] and "Mechanical: Jim (generalist)" in r["text"]
    assert D.chip_text({"label": "Documentation", "via": "none", "members": []}) == "Documentation: nobody yet"


def test_generalist_approvals_count_for_any_discipline_unless_a_specialist_is_required():
    pol = {"specialist_required": {"production": ["electronics"]}}
    assert D.eligible(M["jim"], "electronics", M, pol, "pilot") == "generalist"
    assert D.eligible(M["jim"], "electronics", M, pol, "production") is None
    assert D.eligible(M["ana"], "electronics", M, pol, "production") == "specialist"
    assert D.covers(M["jim"], ["electronics", "manufacturing"], M, pol, "production") == ["manufacturing"]
    assert D.approve_label(["electronics", "manufacturing"]) == "Approve as Electronics and Manufacturing"
    assert D.eligible(M["vic"], "documentation", M, {}, "pilot") is None          # viewers only with the policy
    assert D.eligible(M["vic"], "documentation", M, {"viewers_can_approve": True}, "pilot") == "specialist"
    assert D.eligible(M["raj"], "electronics", M, {}, "pilot") is None
    assert D.eligible({**M["ana"], "revoked": True}, "electronics", M, {}, "pilot") is None


def test_watch_all_changes_is_on_for_generalists_and_settable():
    assert D.watching(M["jim"]) and not D.watching(M["ana"])
    assert D.watching({**M["ana"], "watch": True}) and not D.watching({**M["jim"], "watch": False})
    r = D.routing({"fab.process": "cnc_router"}, author="raj", members={**M, "ana": {**M["ana"], "watch": True}})
    assert "ana" in r["watchers"] and "raj" not in r["watchers"]


def test_discipline_tags_are_validated():
    assert D.validate(["sourcing", "electronics", "electronics"]) == ["electronics", "sourcing"]
    assert D.validate("generalist, mechanical") == ["mechanical", "generalist"]
    for bad in (["Electronics"], ["x"], 5):
        try:
            D.validate(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
