"""The servo rail: one voltage per arm, decided with the sizing, and every torque margin, the simulation's joint
efforts and the electronics worked out at it; a rail the user asks for is used when every servo takes it."""

import json

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


# ------------------------------------------------------------------ one voltage class per arm (Present step 12)

def test_swapping_the_so101_to_dynamixel_keeps_one_voltage_class():
    """'Swap to Dynamixel servos' put XL430s (6.5-12 V) and XL330s (3.7-6 V) on one 6 V rail: infeasible, with a margin
    worked out at 12 V and firmware for a rail below the XL430's minimum."""
    d = template("so101", "so101").with_changes(intent.interpret("Swap to Dynamixel servos", template("so101", "so101"))["changes"])
    sz = imported.size(d)
    assert {j["actuator"] for j in sz["joints"]} | {sz["gripper_actuator"]} == {"xl430_w250"}
    ep = electronics2.plan(d, sz)
    assert ep["supply"]["servo_v"] == 12.0 and ep["issues"] == [] and not [w for w in ep["warnings"] if "rated" in w]
    assert next(j for j in sz["joints"] if j["name"] == "shoulder_lift")["margin"] == 2.27
    asm = imported.build(d, sz, ep)
    assert len(asm.meta["issues"]) == 6 and all("printed holder must be resized" in i for i in asm.meta["issues"])


def test_a_rail_outside_a_servos_range_gets_no_wiring_or_firmware(tmp_path):
    d = template("so101", "so101").with_changes({"arm.actuator_family": "dynamixel", "arm.actuator_overrides": "wrist_flex=xl330_m288"})
    sz = imported.size(d)
    ep = electronics2.plan(d, sz)
    assert any("incompatible supply voltages" in i for i in ep["issues"])
    bad = [i for i in ep["issues"] if "no wiring or firmware is made for it" in i]
    assert bad and all(("xl430_w250 takes 6.5-12 V" in i) or ("xl330_m288 takes 3.7-6 V" in i) for i in bad), ep["issues"]
    asm = imported.build(d, sz, ep)
    with pytest.raises(ValueError, match="voltage range"):
        electronics2.firmware(d, ep, asm, str(tmp_path / "fw"), "r001")
    with pytest.raises(ValueError, match="voltage range"):
        electronics2.finalize(d, sz, ep, asm, str(tmp_path / "r001" / "electronics"))
    assert not [f for _, _, fs in __import__("os").walk(tmp_path) for f in fs]


def test_a_custom_pcb_never_passes_12_v_to_74_v_servos():
    d = template("so101", "so101").with_changes({"electronics.servo_v": 7.4, "electronics.config": "custom_pcb"})
    ep = electronics2.plan(d, imported.size(d))
    assert any("custom PCB passes its input straight" in i for i in ep["issues"])
    assert any(i.endswith("no wiring or firmware is made for it") and "takes 4.8-7.4 V but its servo rail is 12 V" in i
               for i in ep["issues"]), ep["issues"]


def test_the_present_caption_says_what_the_dynamixel_swap_does():
    import os

    path = os.path.join(os.path.dirname(arm.__file__), "..", "studio", "static", "present.js")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    step = text.split("title: 'Edit someone else’s robot'", 1)[1].split("},", 1)[0]
    assert "Six XL430s" in step and "one 12 V supply" in step and "not feasible" in step and "orange" in step


@pytest.mark.parametrize("project, msg, servo_v, built, charged", [
    # the 12 V STS3215s take a charged 2S pack (8.4 V); the SO-101's 7.4 V ones don't
    ("egg-picker", "run it from a 7.4 V battery", 7.4, "a 12 V 10 A mains supply and the servo buck set to 7.4 V", None),
    ("egg-picker", "power it from a 2S lipo", 7.4, "a 12 V 10 A mains supply and the servo buck set to 7.4 V", None),
    ("so101", "run it from a 7.4 V battery", 7.4, "a 12 V 10 A mains supply and the servo buck set to 7.4 V",
     "a charged 2S lithium pack reads 8.4 V, above the STS3215-7V4's 7.4 V maximum, so don't wire one straight to the "
     "servos"),
    ("so101", "run it from a battery", None, "a 6 V 10 A mains supply straight to its servos", None),
])
def test_a_battery_request_says_what_the_robot_is_built_with(project, msg, servo_v, built, charged):
    # "run it from a 7.4 V battery" set a 7.4 V servo rail and built a 12 V mains supply and a servo buck in silence
    d = template(project, project) if project == "so101" else template(project)
    r = intent.interpret(msg, d)
    assert r["changes"] == ({"electronics.servo_v": servo_v} if servo_v else {})
    note = f"Morph builds robots for mains power, not batteries, so this one gets {built}, with no battery, charger or " \
           "low-voltage cutoff"
    assert note in r["reply"]
    assert (charged in r["reply"]) if charged else "lithium pack" not in r["reply"]
    # and the build does what the reply says
    nd = d.with_changes(r["changes"])
    sup = electronics2.plan(nd, imported.size(nd) if nd.arm.source else arm.size(nd))["supply"]
    assert f"a {sup['v']:g} V {sup['a']:g} A mains supply" in built and sup["servo_buck"] == ("buck" in built)


def test_a_battery_request_answered_by_claude_still_says_what_is_built(monkeypatch):
    import sys
    import types

    from morph import settings

    class Client:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            assert "Morph plans mains power only" in json.dumps(kw["tools"])
            use = types.SimpleNamespace(type="tool_use", name=intent.TOOL["name"],
                                        input={"changes": {"electronics.servo_v": 7.4}, "reply": "Running it from 7.4 V."})
            return types.SimpleNamespace(stop_reason="tool_use", content=[use])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setattr(settings, "network_allowed", lambda: True)
    r = intent.interpret("run it from a 7.4 V battery", template("egg-picker"))
    assert r["source"] == "claude" and r["changes"] == {"electronics.servo_v": 7.4}
    assert r["reply"].startswith("Running it from 7.4 V. (Morph builds robots for mains power, not batteries, so this one "
                                 "gets a 12 V 10 A mains supply and the servo buck set to 7.4 V")
