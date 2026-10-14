"""Capability envelope (RELEASE_CONTRACTS section 6): numbers come from the generator, checks and clamping."""

import pytest

from morph.robot.pipeline2 import PARAMS
from morph.studio import capability as cap


def test_constants_match_the_generator():
    """derive() sizes the reference robot with arm.size() and the gripper with arm._grip_plan(); if this fails the generator
    changed: update PAYLOAD_G / MIN_WIDTH_MM (and the contract table) from the printed values."""
    d = cap.derive()
    assert d["payload_g"] == cap.PAYLOAD_G, d["payload_g"]
    assert d["min_width_mm"] == cap.MIN_WIDTH_MM, d["min_width_mm"]
    lo, hi = d["reach_mm"]
    assert abs(lo - cap.REACH_MM[0]) <= 15 and abs(hi - cap.REACH_MM[1]) <= 25, d["reach_mm"]


def test_contract_table():
    assert cap.PAYLOAD_G == {"hobby_pwm": {4: 100, 5: 100, 6: 0}, "smart_bus": {4: 300, 5: 200, 6: 100},
                             "dynamixel": {4: 500, 5: 300, 6: 200}}
    assert cap.min_width("cylinder") == 26.0 and cap.min_width("box") == 21.5 and cap.MAX_WIDTH_MM == 75.0
    assert cap.min_width("sphere", "none") == 38.0 and cap.MAX_MASS_G == 500


def test_max_width_is_the_stroke_check():
    from morph import analysis
    from morph.design import build_profile
    from morph.spec import Design

    d = Design()
    assert d.gripper.stroke_mm - 10 == cap.MAX_WIDTH_MM
    issues = lambda w: analysis.analyze(d.with_changes({"object.diameter_mm": w}),  # noqa: E731
                                        build_profile(d.with_changes({"object.diameter_mm": w})))["issues"]
    assert not any("stroke" in i for i in issues(75.0)) and any("stroke" in i for i in issues(75.5))


def test_envelope_and_model_text():
    e = cap.envelope()
    assert e["dof"] == [4, 5, 6] and e["payload_g"]["dynamixel"]["4"] == 500 and e["object"]["width_mm"]["max"] == 75.0
    assert e["table_mm"] == {"x": [80, 400], "y": [-300, 300]} and e["reach_mm"]["param"] == [200, 500]
    assert set(e["params"]) == {p[0] for p in PARAMS}
    assert "no vision code" in e["firmware"] and any("drone" in x for x in e["out_of_scope"])
    t = cap.describe_for_models()
    for frag in ("4, 5 or 6 joints", "26 to 75 mm", "500 g", "SO-101", "no vision code", "Out of scope", "pouring"):
        assert frag in t, frag
    assert "claude-" not in t and "subq-" not in t  # no model identifiers in prompts built from the envelope


@pytest.mark.parametrize("changes,status", [
    ({}, "ok"),
    ({"object.mass_g": 2000}, "out_of_scope"),
    ({"object.diameter_mm": 16}, "out_of_scope"),
    ({"object.diameter_mm": 100}, "out_of_scope"),
    ({"object.diameter_mm": 30, "finger.sensor": "none"}, "adjusted"),
    ({"object.diameter_mm": 30, "finger.sensor": "fsr402"}, "ok"),
    ({"object.mass_g": 370, "object.max_force_n": 0}, "adjusted"),
    ({"arm.dof": 7}, "adjusted"),
    ({"task.cycle_time_s": 1.0}, "adjusted"),
    ({"task.pick_x_mm": 900}, "adjusted"),
    ({"arm.source": "so101", "object.mass_g": 300}, "adjusted"),
    ({"arm.source": "../../etc"}, "out_of_scope"),
])
def test_check(changes, status):
    r = cap.check(changes)
    assert r["status"] == status, r
    assert (r["nearest"] is None) == (status == "ok")
    if r["nearest"]:
        assert cap.check({**changes, **r["nearest"]})["status"] in ("ok", "adjusted")


def test_check_nearest_for_heavy_objects_is_inside():
    r = cap.check({"object.mass_g": 370, "object.max_force_n": 0})
    assert r["nearest"] == {"arm.actuator_family": "dynamixel", "arm.dof": 4}
    assert cap.check({"object.mass_g": 370, **r["nearest"]})["status"] == "ok"


def test_clamp_drops_what_models_may_not_set():
    out, notes = cap.clamp({"arm.dof": 7, "arm.reach_mm": 550, "name": "../x", "arm.source": "../../etc", "foo.bar": 1,
                            "electronics.extras": "wrist_camera,laser,estop", "task.cycle_time_s": 1, "arm.actuator_family": "banana",
                            "object.mass_g": "300", "object.diameter_mm": float("nan"), "build.quantity": 10 ** 9,
                            "arm.actuator_overrides": "j2=xm430_w350"})
    assert out == {"arm.dof": 6, "arm.reach_mm": 500.0, "electronics.extras": "wrist_camera,estop", "task.cycle_time_s": 4.0,
                   "object.mass_g": 300.0, "build.quantity": 10000}
    assert len(notes) >= 8
    ok, _ = cap.clamp({"name": "egg-picker", "arm.source": "so101"}, allow=("name", "arm.source"))
    assert ok == {"name": "egg-picker", "arm.source": "so101"}
    bad, _ = cap.clamp({"arm.source": "../x"}, allow=("arm.source",))
    assert bad == {}


def test_warm_is_safe_in_the_background():
    cap.warm(block=False)
    cap.warm(block=True)
    assert cap.envelope()["source"] == "code" and cap.envelope()["payload_g"]["smart_bus"]["5"] == 200


def test_the_so101_payload_is_the_builds():
    """New robot's 'the stock SO-101 lifts about N g' is what imported.size() accepts at the SO-101's servo rail."""
    assert cap.derive(so101=True)["so101_payload_g"] == cap.SO101_PAYLOAD_G
