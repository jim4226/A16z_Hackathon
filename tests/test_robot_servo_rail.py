"""The servo rail: one voltage per arm, decided with the sizing, and every torque margin, the simulation's joint
efforts and the electronics worked out at it; a rail the user asks for is used when every servo takes it."""

import pytest

from morph.robot import arm, catalog, electronics2, imported
from morph.studio import intent
from morph.studio.projects import template


def _sized(d):
    return imported.size(d) if d.arm.source else arm.size(d)


def test_stall_torque_follows_the_datasheet_points_and_the_supply_voltage():
    assert catalog.torque_at("xl430_w250", 11.1) == 1.4 and catalog.torque_at("xl430_w250", 10.05) == pytest.approx(1.2)
    assert catalog.torque_at("sts3215_7v4", 6.0) == 1.62               # 16.5 kg·cm at 6 V, 19.5 at 7.4 V
    assert catalog.torque_at("ds3218", 6.0) == pytest.approx(2.1 * 6.0 / 6.8)   # one point: in proportion below it
    assert catalog.torque_at("mg996r", 7.2) == 1.08                    # never more than its rating above it
    for key, a in catalog.ACTUATORS.items():
        assert catalog.torque_at(key, a["nominal_v"]) == a["torque_nm"], key


@pytest.mark.parametrize("name, design, rail, supply, margins", [
    ("egg-picker", lambda: template("egg-picker"), 12.0, "psu_12v20a", {"j2_shoulder": 3.61}),
    # the SG90 on the wrist roll stops at 6 V, so the DS3218s (rated 6.8 V) give 88 % of their torque
    ("hobby egg-picker", lambda: template("egg-picker").with_changes({"arm.actuator_family": "hobby_pwm"}), 6.0,
     "psu_6v20a", {"j2_shoulder": 2.51, "j3_elbow": 2.54}),
    # no catalogued supply gives 7.4 V, and 6 V keeps 85 % of the STS3215-C001's torque (16.5 of 19.5 kg·cm)
    ("so101", lambda: template("so101", "so101"), 6.0, "psu_6v", {"shoulder_lift": 2.45, "elbow_flex": 2.85}),
])
def test_margins_and_simulation_efforts_are_worked_out_at_the_servo_rail(name, design, rail, supply, margins):
    d = design()
    sz = _sized(d)
    assert sz["servo_rail"]["v"] == rail
    joints = {j["name"]: j for j in sz["joints"]}
    for jn, m in margins.items():
        assert joints[jn]["margin"] == m, (jn, joints[jn])
    ep = electronics2.plan(d, sz)
    assert ep["supply"]["key"] == supply and ep["supply"]["servo_v"] == rail and not ep["supply"]["servo_buck"]
    derated = [w for w in ep["warnings"] if "gives about" in w]
    assert bool(derated) == (name != "egg-picker"), ep["warnings"]
    asm = (imported.build if d.arm.source else arm.build)(d, sz, ep)
    eff = {j.name: j.effort for j in asm.joints if j.name in joints}
    for jn, j in joints.items():
        assert eff[jn] == pytest.approx(j["torque_avail_nm"], abs=0.002), jn   # the simulation's servos too


def test_a_hand_written_profile_says_its_margins_are_at_q0_and_what_its_worst_pose_needs():
    sz = imported.size(template("so101", "so101"))
    assert sz["basis"] == "q = 0"
    w = [x for x in sz["warnings"] if x.startswith("shoulder_lift:")]
    assert len(w) == 1 and "1.01 N·m" in w[0] and "1.61x margin at 6 V" in w[0], sz["warnings"]


def test_chat_supply_voltage_changes_the_rail_or_says_why_not():
    egg = template("egg-picker")
    r = intent.interpret("use a 5 V supply", egg)
    assert r["changes"] == {} and "STS3215 takes 6-12.6 V" in r["reply"] and "stays at 12 V" in r["reply"]
    so = template("so101", "so101")
    r = intent.interpret("use a 5 V supply", so)
    assert r["changes"] == {"electronics.servo_v": 5.0}
    d = so.with_changes(r["changes"])
    sz = imported.size(d)
    ep = electronics2.plan(d, sz)
    assert sz["servo_rail"]["v"] == ep["supply"]["servo_v"] == 5.0
    # a 4 A 5 V supply can't carry the arm's peak current: a 12 V supply and the servo buck set to 5 V instead
    assert ep["supply"]["servo_buck"] and ep["supply"]["v"] == 12.0
    lift = next(j for j in sz["joints"] if j["name"] == "shoulder_lift")
    assert lift["torque_avail_nm"] == pytest.approx(1.62 * 5 / 6, abs=0.001)
    assert intent.interpret("use the rated supply voltage", d)["changes"] == {"electronics.servo_v": 0.0}


def test_a_rail_no_servo_takes_is_kept_out_and_said(tmp_path):
    d = template("egg-picker").with_changes({"electronics.servo_v": 5.0})    # set by Claude or a script, not the rules
    sz = arm.size(d)
    ep = electronics2.plan(d, sz)
    assert ep["supply"]["servo_v"] == 12.0
    assert any(w.startswith("a 5 V servo supply was asked for, but the sts3215 takes 6-12.6 V") and "stays at 12 V" in w
               for w in ep["warnings"]), ep["warnings"]
    asm = arm.build(d, sz, ep)
    code = (tmp_path / electronics2.firmware(d, ep, asm, str(tmp_path), "r001")).read_text(encoding="utf-8")
    assert "#define VSERVO_NOMINAL   12.00f" in code


def test_the_firmware_and_the_supply_follow_the_rail(tmp_path):
    d = template("egg-picker").with_changes({"electronics.servo_v": 7.4})
    sz = arm.size(d)
    ep = electronics2.plan(d, sz)
    assert ep["supply"] == {**ep["supply"], "servo_v": 7.4, "servo_buck": True, "v": 12.0}
    j2 = next(j for j in sz["joints"] if j["name"] == "j2_shoulder")
    assert j2["torque_avail_nm"] == pytest.approx(2 * 0.95 * 2.9 * 7.4 / 12, abs=0.001)
    asm = arm.build(d, sz, ep)
    code = (tmp_path / electronics2.firmware(d, ep, asm, str(tmp_path), "r001")).read_text(encoding="utf-8")
    assert "#define VSERVO_NOMINAL   7.40f" in code


def test_asking_for_the_so101s_rated_voltage_gives_it_through_the_servo_buck():
    so = template("so101", "so101")
    r = intent.interpret("use a 7.4 V supply", so)
    d = so.with_changes(r["changes"])
    sz = imported.size(d)
    ep = electronics2.plan(d, sz)
    assert ep["supply"]["servo_v"] == 7.4 and ep["supply"]["servo_buck"] and ep["supply"]["v"] == 12.0
    assert next(j for j in sz["joints"] if j["name"] == "shoulder_lift")["margin"] == 2.89
    assert not [w for w in ep["warnings"] if "gives about" in w]
