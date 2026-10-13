"""Chat edits without a Claude key (the built-in rules), as people type them in an open project: "make it cheaper",
"faster", a change that is already there, questions and thanks. Offline: interpret() never reaches a model here."""

import pytest

from morph import settings
from morph.spec import Design
from morph.studio import capability, intent


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr(settings, "network_allowed", lambda: False)  # the rules alone, as with no key


def egg_arm(**ch):
    return Design().with_changes({"object.name": "raw egg", "object.mass_g": 60.0, **ch})


def say(msg, d):
    out = intent.interpret(msg, d)
    assert out["source"] == "rules"
    return out


def test_make_it_cheaper_steps_down_while_the_object_still_fits():
    d = egg_arm(**{"arm.actuator_family": "dynamixel", "arm.dof": 6})
    seen = []
    for _ in range(5):
        out = say("make it cheaper", d)
        if not out["changes"]:
            break
        seen.append(out["changes"])
        d = d.with_changes(out["changes"])
        assert capability.payload_g(d.arm.actuator_family, d.arm.dof) >= 60  # every step still lifts it with a 2x margin
    assert seen == [{"arm.actuator_family": "smart_bus"}, {"arm.dof": 5}, {"arm.dof": 4}, {"arm.actuator_family": "hobby_pwm"}]
    assert out["reply"] == ("Nothing to change: this is already the cheapest arm Morph makes for a 60 g object "
                            "(4 joints, hobby PWM servos).")


def test_cheaper_never_drops_below_the_object():
    d = egg_arm(**{"object.mass_g": 250.0, "arm.dof": 5})                    # smart bus, 5 joints: 200 g at 2x
    assert say("lower the cost", d)["changes"] == {"arm.dof": 4}             # 4 joints lift 300 g
    d = d.with_changes({"arm.dof": 4})
    out = say("reduce the cost", d)                                          # hobby PWM at 4 joints: 200 g, too little
    assert out["changes"] == {} and "already the cheapest" in out["reply"]
    assert say("make it cheaper", egg_arm(**{"arm.source": "so101"}))["changes"] == {}  # an imported arm's own servos


def test_faster_and_slower_move_the_cycle_inside_the_envelope():
    d = egg_arm(**{"task.cycle_time_s": 6.0})
    out = say("make it faster", d)
    assert out["changes"] == {"task.cycle_time_s": 4.5} and "was 6 s" in out["reply"]
    d = d.with_changes({"task.cycle_time_s": capability.CYCLE_S[0]})
    out = say("faster please", d)
    assert out["changes"] == {} and out["reply"].startswith("Nothing to change: one pick and place every 4 s")
    assert say("slow it down", egg_arm(**{"task.cycle_time_s": 6.0}))["changes"] == {"task.cycle_time_s": 8.0}
    assert say("use faster servos", d)["changes"] == {}  # about servos, not the pace


def test_a_change_that_is_already_there_says_so():
    d = egg_arm(**{"electronics.extras": "estop"})
    assert say("add an e-stop", d) == {"changes": {}, "source": "rules", "last_target": None,
                                       "reply": "Nothing to change: it already has an emergency stop."}
    assert say("remove the imu", d)["reply"] == "Nothing to change: it has no IMU."
    out = say("add an e-stop and make it 6-DOF", d)
    assert out["changes"] == {"arm.dof": 6} and out["reply"] == "Changing 6-DOF arm. (It already has an emergency stop.)"


def test_questions_and_thanks_are_not_failed_edits():
    d = egg_arm()
    out = say("can it lift 2 kg?", d)
    assert out["changes"] == {} and out["reply"].startswith("That reads like a question, so nothing was changed: without Claude")
    assert say("thanks", d)["reply"].startswith("Ask for a change whenever you're ready")
    out = say("make it purple", d)
    assert out["reply"].startswith("I couldn't map that to a design parameter.") and "“make it cheaper”" in out["reply"]


def test_routing_keeps_cheaper_as_an_edit():
    from morph.studio import agents
    assert agents.chat_offer("make it cheaper", egg_arm(), {"name": "egg-picker"}) is None
    assert agents.chat_offer("cheaper", egg_arm(), {"name": "egg-picker"}) is None
