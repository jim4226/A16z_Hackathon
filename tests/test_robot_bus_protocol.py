"""One servo bus speaks one command protocol: a Dynamixel on a Feetech bus (or the reverse) is a plan issue, never
firmware that sends it the wrong commands; an actuator override the catalog doesn't know is an issue too."""

import os

import pytest

from morph.cli import EGG_ROBOT
from morph.robot import arm, catalog
from morph.robot import electronics2 as e2
from morph.spec import Design


@pytest.fixture(scope="module", autouse=True)
def _pcb_cache(tmp_path_factory):
    os.environ.setdefault("MORPH_PCB_CACHE", str(tmp_path_factory.mktemp("pcb_cache")))


def _design(fam="smart_bus", overrides="", cfg="modules"):
    return Design().with_changes({**EGG_ROBOT, "arm.actuator_family": fam, "arm.actuator_overrides": overrides,
                                  "electronics.config": cfg})


def _plan(d):
    s = arm.size(d)
    return s, e2.plan(d, s)


def test_every_actuator_names_its_protocol():
    for key in catalog.ACTUATORS:
        assert catalog.protocol(key) in catalog.PROTOCOLS, key
    assert catalog.protocol("xm430_w350") == catalog.protocol("xl330_m288") == "dynamixel_2"
    assert catalog.protocol("sts3215") == catalog.protocol("sts3215_7v4") == "feetech_scs"
    assert catalog.protocol("mg996r") == "pwm" and catalog.protocol("nonexistent") is None
    for fam, keys in catalog.FAMILY_ACTUATORS.items():   # a family's own actuators all speak its bus protocol
        bus, proto, _ = e2.FAMILY_BUS[fam]
        assert {catalog.protocol(k) for k in keys} == {e2.SPEAKS[proto]}, fam


@pytest.mark.parametrize("fam, overrides, joint", [
    ("smart_bus", "j2=xm430_w350", "j2_shoulder"),        # Dynamixel on the Feetech bus
    ("smart_bus", "gripper=xl430_w250", "gripper"),       # also for the gripper: never swapped behind the user's back
    ("dynamixel", "j3=sts3215", "j3_elbow"),              # Feetech on the Dynamixel bus
])
def test_mixed_protocol_bus_is_an_issue_and_gets_no_firmware(tmp_path, fam, overrides, joint):
    d = _design(fam, overrides)
    s, p = _plan(d)
    assert any(a["joint"] == joint and a["key"] == overrides.split("=")[1] for a in p["actuators"])
    bad = [i for i in p["issues"] if i.startswith(joint + ":")]
    assert len(bad) == 1 and "protocol" in bad[0], p["issues"]
    a = arm.build(d, s, p)
    with pytest.raises(ValueError, match="protocol"):
        e2.firmware(d, p, a, str(tmp_path / "fw"), "r001")
    with pytest.raises(ValueError, match="protocol"):
        e2.finalize(d, s, p, a, str(tmp_path / "r001" / "electronics"))
    written = [f for _, _, fs in os.walk(tmp_path) for f in fs]
    assert not any(f.endswith(".ino") for f in written), written


def test_one_protocol_per_bus_still_builds(tmp_path):
    d = _design("dynamixel", "j2=xl430_w250,j4=xm430_w350")     # two Dynamixel models share protocol 2.0
    s, p = _plan(d)
    assert not [i for i in p["issues"] if "protocol" in i]
    a = arm.build(d, s, p)
    fw = e2.firmware(d, p, a, str(tmp_path / "fw"), "r001")
    code = open(tmp_path / "fw" / fw, encoding="utf-8").read()
    assert "#include <Dynamixel2Arduino.h>" in code and "SCServo" not in code


def test_unknown_override_is_an_issue_not_ignored():
    s = arm.size(_design(overrides="j3=nonexistent"))
    assert any("nonexistent" in i and "j3" in i for i in s["issues"]), s["issues"]
    s = arm.size(_design(overrides="j9=sts3215"))               # a joint this 5-DOF arm doesn't have
    assert any("j9" in i for i in s["issues"]), s["issues"]
    s = arm.size(_design(overrides="j2:xm430_w350"))            # not joint=actuator
    assert any("j2:xm430_w350" in i for i in s["issues"]), s["issues"]
    s = arm.size(_design("dynamixel", "j2=xm430_w350, j4_wrist_pitch=xl430_w250,gripper=xl430_w250"))
    assert s["issues"] == []


def test_pipeline_marks_a_mixed_bus_infeasible(tmp_path):
    from morph.robot.pipeline2 import RobotProject

    p = RobotProject(str(tmp_path / "mixed"))
    r = p.run(_design(overrides="j2=xm430_w350"), n_trials=1, verbose=False, fast_sim=True)
    res = p.load_results(r)
    s = res["summary"]
    assert s["feasible"] is False and "protocol" in s["issues"][0]
    assert res["stage_status"]["wiring"] == "error" and "mixes protocols" in res["errors"]["wiring"]
    assert not [f for _, _, fs in os.walk(p.rev_dir(r)) for f in fs if f.endswith(".ino")]
