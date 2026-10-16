"""Conversational brief engine, offline: a labelled eval of one-liners, the follow-up policy, chips inside the envelope,
harm refusals, units, mapping, preflight + repair, drafts and the build hand-off. No keys, no network."""

import json
import os
import time

import pytest

from morph.spec import Design
from morph.studio import agents
from morph.studio import brief as B
from morph.studio import capability as cap


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)


# (one-liner, archetype, route, first-turn status, (min, max) questions, scope)
EVAL = [
    # the 23 research one-liners
    ("a robot that waters my plants", "plant_watering", "new_robot", "asking", (1, 3), "adjusted"),
    ("help me sort socks", "soft_goods", "new_robot", "asking", (1, 2), "ok"),
    ("something to pick strawberries", "fragile_food", "new_robot", "asking", (1, 2), "ok"),
    ("a robot for my lab that moves test tubes", "lab_transfer", "new_robot", "asking", (1, 3), "adjusted"),
    ("make me a robot arm", "generic_arm", "new_robot", "asking", (2, 3), "ok"),
    ("I need a robot", "generic_arm", "new_robot", "asking", (2, 3), "ok"),
    ("a robot that feeds my cat", "pet_feeding", "new_robot", "asking", (1, 3), "adjusted"),
    ("a robot to load my dishwasher", "dishes", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("pick up eggs and put them in a carton", "fragile_food", "new_robot", "asking", (1, 2), "ok"),
    ("a robot that sorts my lego bricks by colour", "small_parts", "new_robot", "asking", (1, 3), "adjusted"),
    ("build a robot to stack boxes in my garage, about 2 kg each", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot that hands me tools while I work", "tool_handover", "new_robot", "asking", (1, 3), "ok"),
    ("a robot that can pour a glass of water", "containers", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("pick ping pong balls off the floor", "toys", "new_robot", "asking", (1, 2), "ok"),
    ("a cheap robot arm under $200", "generic_arm", "new_robot", "asking", (1, 3), "adjusted"),
    ("a mobile robot that vacuums", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a drone that delivers packages", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a humanoid that folds laundry", "soft_goods", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot that picks up a 5 kg dumbbell", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot to sort recycling: cans and bottles", "containers", "new_robot", "asking", (1, 2), "ok"),
    ("move pens from one cup to another", "desk_items", "new_robot", "asking", (1, 3), "adjusted"),
    ("a robot that can pick up a pencil", "desk_items", "new_robot", "asking", (1, 3), "adjusted"),
    ("a 6 axis arm with a camera that sorts screws", "small_parts", "new_robot", "asking", (1, 2), "adjusted"),
    # the rest of the release eval
    ("build a robot that shoots nerf darts at my brother", None, None, "refused", (0, 0), "out_of_scope"),
    ("a robot that restrains my dog", None, None, "refused", (0, 0), "out_of_scope"),
    ("build a gun turret for my yard", None, None, "refused", (0, 0), "out_of_scope"),
    ("an automatic cat feeder", "pet_feeding", "new_robot", "asking", (1, 3), "adjusted"),
    ("dishwasher loading robot", "dishes", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("lego sorting machine", "small_parts", "new_robot", "asking", (1, 3), "adjusted"),
    ("a tool hand-over arm for my workbench", "tool_handover", "new_robot", "asking", (1, 3), "ok"),
    ("I have an SO-101, can it sort strawberries?", "so101", "new_robot", "asking", (0, 2), "ok"),
    ("use my LeRobot SO-100 to pick up eggs", "so101", "new_robot", "asking", (0, 2), "ok"),
    ("pick tomatoes from a tray into a box, 10 per minute", "fragile_food", "new_robot", "asking", (0, 1), "ok"),
    ("a precise arm that moves 30 mm glass vials, about ±1 mm", "lab_transfer", "new_robot", "asking", (0, 2), "ok"),
    ("an arm on wifi that moves cups of cat food", "pet_feeding", "new_robot", "asking", (0, 2), "adjusted"),
    ("a 4 dof arm with hobby servos to pick up ping pong balls", "toys", "new_robot", "asking", (0, 2), "ok"),
    ("a robot arm for 1000 units that packs chocolates", "fragile_food", "new_robot", "asking", (1, 2), "ok"),
    ("an arm that stacks wooden blocks for my kids", "toys", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot that flips pancakes", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot with a suction cup to pick up phones", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a robot that sorts grapes", "fragile_food", "new_robot", "asking", (1, 3), "adjusted"),
    ("a robot that picks up 300 g cans", "containers", "new_robot", "asking", (1, 2), "ok"),
    ("a robot that picks up 10 cm boxes", "generic_arm", "new_robot", "out_of_scope", (0, 0), "out_of_scope"),
    ("a 6 axis dynamixel arm with a custom pcb that moves eggs", "fragile_food", "new_robot", "asking", (0, 1), "ok"),
    ("a gentle gripper arm for light bulbs", "generic_arm", "new_robot", "asking", (1, 2), "ok"),
    ("pick strawberries without hurting them", "fragile_food", "new_robot", "asking", (1, 2), "ok"),
    ("an egg sorting robot for our kitchen", "fragile_food", "new_robot", "asking", (1, 2), "ok"),
    ("hello", "generic_arm", "other", "asking", (3, 3), "ok"),
]


def test_eval_has_enough_cases():
    assert len(EVAL) >= 40 and len({e[0] for e in EVAL}) == len(EVAL)


@pytest.mark.parametrize("text,arch,route,status,qs,scope", EVAL, ids=[e[0][:40] for e in EVAL])
def test_one_liner(text, arch, route, status, qs, scope):
    b = agents.run_turn(text=text)
    assert b.status == status, (b.status, b.scope)
    assert qs[0] <= len(b.questions) <= qs[1], [q["slot"] for q in b.questions]
    assert b.scope["status"] == scope, b.scope
    if status == "refused":
        assert not b.changes and not b.questions and "Morph doesn't design that" in b.scope["notes"][0]
        return
    assert b.archetype["key"] == arch, b.archetype
    assert b.route["value"] == route, b.route
    assert len(b.questions) <= B.MAX_QUESTIONS and all(2 <= len(q["chips"]) <= 4 for q in b.questions)
    if status == "out_of_scope":
        assert b.scope["nearest"] and b.scope["nearest"]["summary"] and b.scope["notes"]
    # "Use sensible defaults" (or "Build the nearest robot") always ends ready, and the design passes preflight after repair
    b = agents.run_turn(b, action="defaults")
    assert b.status == "ready", b.scope
    assert b.preflight["issues"] == [], (text, b.preflight, b.changes)
    assert B.NAME_RE.match(b.name) and b.summary and b.changes["name"]
    assert cap.check(b.changes)["status"] in ("ok", "adjusted")
    Design().with_changes(b.changes)  # every key exists and coerces
    if scope != "ok":
        assert any(a["kind"] == "scope" for a in b.assumptions)


def test_questions_follow_the_policy():
    b = agents.run_turn(text="make me a robot arm")
    assert [q["slot"] for q in b.questions][0] == "object" and b.round == 1
    assert all(any(c.get("default") for c in q["chips"]) for q in b.questions)
    agents.run_turn(b, answers={"object": "strawberry"})  # required slot answered: no more rounds
    assert b.status == "ready" and b.round == 1 and b.archetype["key"] == "fragile_food"
    b = agents.run_turn(text="I need a robot")
    agents.run_turn(b, answers={"task": "sort"})          # object still open: ask it again, alone
    assert b.status == "asking" and b.round == 2 and [q["slot"] for q in b.questions] == ["object"]
    agents.run_turn(b, answers={"layout": "short_hop"})
    agents.run_turn(b, answers={"budget": 1000})
    assert b.status == "ready" and b.round == B.MAX_ROUNDS  # three rounds at most, then defaults
    assert B.value(b, "perception") == "wrist_camera" and "wrist_camera" in b.changes["electronics.extras"]


def test_very_specific_request_goes_straight_to_confirm():
    b = agents.run_turn(text="pick eggs from a tray into a carton, one every 8 s, people next to me, up to $500, just one")
    assert b.status == "ready" and not b.questions
    assert b.changes["task.cycle_time_s"] == 8.0 and "estop" in b.changes["electronics.extras"]


@pytest.mark.parametrize("text", [
    "build a robot that shoots nerf darts at my brother", "a turret that fires at intruders", "a robot that hurts my neighbour",
    "a robot to restrain people", "a robot that throws rocks at the cat", "a taser arm", "a robot arm holding a pistol",
    "a robot to spy on my roommate", "tie up the dog", "a robot with a gun", "hand me the stun gun",
    "a robot that hands me an air gun", "a robot that points a gun at the door",
])
def test_harm_refused_locally(text):
    assert B.harmful(text)
    b = B.start(text)
    assert b.status == "refused" and not b.changes


@pytest.mark.parametrize("text", [
    "pick strawberries without hurting them", "sort bath bombs into boxes", "a glue gun holder", "an arm with a kill switch",
    "shoot photos of my products", "launch my small business with a sorting robot", "strawberry stalks into a bin",
    "tie down the cables", "a robot that hands me a nail gun", "feed my cat without harming my cat",
    # workshop, shop and kitchen tools called guns: "hand the packer the tape gun" was refused as meant to hurt people
    "hand the packer the tape gun", "pass the rivet gun to the riveter", "a robot that hands me the grease gun",
    "hand me the scan gun", "pass the soldering gun to me", "hand the price gun to the cashier", "pass me the temp gun",
    "hand the mechanic the impact gun", "hand me the screw gun", "pass the barcode gun to the picker",
    "hand me the hot glue gun", "pass the caulking gun to the plumber", "hand me the heat-shrink gun",
])
def test_harm_filter_has_no_false_positives(text):
    assert B.harmful(text) is None


@pytest.mark.parametrize("text", ["hand the packer the tape gun", "hand the price gun to the cashier", "hand me the screw gun"])
def test_a_hand_over_of_a_tool_gun_is_built(text):
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert b.status == "ready" and B.value(b, "task") == "hand_over" and "estop" in b.changes["electronics.extras"]


def test_units_in_requests():
    p = B.parse("pick up a 2 inch cube that weighs half a pound")
    assert p["slots"]["object"]["diameter_mm"] == pytest.approx(50.8) and p["slots"]["object"]["shape"] == "box"
    p = B.parse("move 4 cm balls of 0.1 kg, 12 per minute, budget $1.5k")
    o = p["slots"]["object"]
    assert (o["diameter_mm"], o["mass_g"], o["shape"]) == (40.0, 100.0, "sphere")
    assert p["slots"]["throughput"] == 5.0 and p["slots"]["budget"] == 1500.0
    assert B.parse("every 7 seconds")["slots"]["throughput"] == 7.0
    assert B.parse("200 an hour")["slots"]["throughput"] == 18.0


def test_chips_are_inside_the_envelope_and_build():
    """Every chip of every archetype question is a concrete slot value the generator can build (preflight, after repair)."""
    seen = 0
    for key, a in B.ARCHETYPES.items():
        for slot in ("object",) + a.optional + ("precision", "budget", "connectivity", "quantity", "existing_hardware", "task"):
            b = B.Brief(id=B.new_id(), request=key)
            B.set_archetype(b, key, 1.0, "rules")
            if key == "so101":
                B.set_slot(b, "existing_hardware", "so101")
            q = B.make_question(b, slot)
            assert 2 <= len(q["chips"]) <= 4 and q["text"] and q["why"], (key, slot)
            for c in q["chips"]:
                v = B.coerce(slot, c["value"])
                assert v is not None, (key, slot, c)
                if slot == "object":
                    assert B.fits(v), (key, c)
                    cap_status = cap.check({f"object.{k}": v[k] for k in ("shape", "diameter_mm", "mass_g")} |
                                           {"finger.sensor": "fsr402"})["status"]
                    assert cap_status in ("ok", "adjusted"), (key, c, cap_status)
                if key == "so101" and slot not in ("object", "layout"):
                    continue  # the SO-101 path keeps its own servos; one sizing takes about a second
                b2 = B.Brief.from_dict(b.to_dict())
                B.answer(b2, {slot: c["value"]})
                B.progress(b2, defaults_now=True, deep=False)
                assert b2.status == "ready" and b2.preflight["issues"] == [], (key, slot, c, b2.preflight, b2.scope)
                seen += 1
    assert seen > 150


def test_presets_win_over_guesses():
    b = B.start("pick eggs")
    agents.apply_update(b, agents.BriefUpdate.model_validate(dict(_update(), object_preset=None, object={
        "name": "raw egg", "shape": "sphere", "diameter_mm": 40, "length_mm": 50, "mass_g": 55, "friction": 0.9})))
    assert b.get("object")["max_force_n"] == 25 and b.slots["object"].source == "user"  # the user said eggs; the preset holds
    o = B.sanitize_object({"name": "egg", "diameter_mm": 44, "max_force_n": 3, "friction": 0.9})
    assert o["key"] == "raw egg" and o["max_force_n"] == 25 and o["friction"] == 0.35


def _update(**kw):
    u = dict(route="new_robot", harmful=False, embodiment="desk_arm", unsupported=[], archetype="generic_arm", object_preset=None,
             object=None, fragility=None, task=None, layout=None, throughput_s=None, environment=None, people_nearby=None,
             precision=None, budget_usd=None, perception=None, connectivity=None, quantity=None, existing_hardware=None, stated=[])
    u.update(kw)
    return u


def test_mapping():
    b = B.start("pick strawberries from a tray into a punnet, 10 per minute, kids around, wifi, 100 units")
    b = agents.run_turn(b, action="defaults")
    ch = b.changes
    assert ch["object.name"] == "strawberry" and ch["object.max_force_n"] == 3 and ch["finger.sensor"] == "fsr402"
    assert (ch["task.pick_x_mm"], ch["task.place_y_mm"]) == (220.0, 170.0) and ch["task.cycle_time_s"] == 6.0
    assert ch["electronics.extras"] == "estop,wifi" and ch["build.quantity"] == 100 and ch["build.config"] == "pilot"
    assert ch["arm.reach_mm"] == 320.0 and ch["finger.width_mm"] == 20.0  # never reach (sizing sets it), always 20 mm fingers
    b = agents.run_turn(text="sort small boxes, very precise, 1000 units")
    b = agents.run_turn(b, action="defaults")
    assert b.changes["arm.actuator_family"] == "dynamixel" and b.changes["build.config"] == "production"
    assert b.changes["electronics.config"] == "custom_pcb" and b.estimate is None  # no cheap cost model for production
    b = agents.run_turn(text="I have an SO-101 for picking strawberries")
    b = agents.run_turn(b, action="defaults")
    assert b.changes["arm.source"] == "so101" and b.preflight["issues"] == [] and "SO-101" in b.summary


def test_repairs_are_assumptions():
    b = agents.run_turn(text="a robot that picks up full soda cans", action=None)
    b = agents.run_turn(b, action="defaults")
    kinds = [a["text"] for a in b.assumptions if a["kind"] == "repair"]
    assert b.changes["arm.dof"] == 4 and any("4 joints" in t for t in kinds)            # torque: fewer joints (budget < $1,000)
    assert b.changes["finger.thickness_mm"] == 7.5 and any("7.5 mm" in t for t in kinds)  # deflection: thicker beam
    b = agents.run_turn(text="a robot that picks up full soda cans, budget $1,500")
    b = agents.run_turn(b, action="defaults")
    assert b.changes["arm.actuator_family"] == "dynamixel"                                 # torque: Dynamixel when affordable
    ch, notes, pre = B.repair({**b.changes, "object.diameter_mm": 30.0, "object.shape": "cylinder", "finger.sensor": "none",
                               "object.max_force_n": 0.0}, b)
    assert ch["finger.sensor"] == "fsr402" and any("FSR 402" in n["text"] for n in notes) and pre["issues"] == []


def test_torque_repair_takes_the_cheaper_arm_and_flags_the_budget():
    b = agents.run_turn(agents.run_turn(text="a robot that picks up 450 g candles, budget $1,000"), action="defaults")
    assert (b.changes["arm.actuator_family"], b.changes["arm.dof"]) == ("dynamixel", 4)     # about $1,230, not $1,810 for 5 joints
    over = [a["text"] for a in b.assumptions if "above your $1,000 budget" in a["text"]]
    assert over and "smaller servos" in over[0] and b.estimate["unit_cost"] > 1000 and b.preflight["issues"] == []
    b = agents.run_turn(agents.run_turn(text="a robot that picks up 450 g candles, budget $2,500"), action="defaults")
    assert (b.changes["arm.actuator_family"], b.changes["arm.dof"]) == ("dynamixel", 5)     # affordable: every joint stays
    assert not any("above your" in a["text"] for a in b.assumptions)
    b = agents.run_turn(agents.run_turn(text="something to pick strawberries for under $300"), action="defaults")
    assert b.estimate["unit_cost"] <= 315 and not any("above your" in a["text"] for a in b.assumptions)
    b = agents.run_turn(agents.run_turn(text="pick up 300 g cans and sort them by colour, budget $285"), action="defaults")
    over = [a["text"] for a in b.assumptions if "above your $285 budget" in a["text"]]
    assert b.changes["arm.actuator_family"] == "smart_bus" and len(over) == 1 and over[0].endswith("above your $285 budget.")  # no reason


def test_custom_objects_are_named_and_their_gaps_are_assumptions():
    b = agents.run_turn(text="a robot that picks up 450 g candles")
    o = b.get("object")
    assert (o["name"], o["key"], o["mass_g"]) == ("candle", None, 450.0) and B.suggest_name(b) == "candle-picker"
    row = next(r for r in b.to_dict()["card"] if r["slot"] == "object")
    assert row["text"] == "Candles (Ø45 mm, 450 g); width assumed" and row["badge"] == "you said"
    names = [B.parse(t)["slots"]["object"]["name"] for t in ("move candles weighing 300 g", "pick up 70 mm glass cups",
                                                              "pick up 120 g potatoes", "pick up 200 g items at 10 per minute")]
    assert names == ["candle", "glass cup", "potato", "object"]
    b = agents.run_turn(b, action="defaults")
    gap = [a for a in b.assumptions if a["slot"] == "object" and a["kind"] == "default"]
    assert gap[0]["text"] == "I assumed the candles are 45 mm wide where the fingers close."   # only the weight was given
    assert [x["label"] for x in gap[0]["options"]] == ["30 mm wide", "55 mm wide", "70 mm wide"]
    b = agents.run_turn(b, answers={"object": gap[0]["options"][2]["value"]})                # the confirm card's chip
    assert b.status == "ready" and b.get("object")["diameter_mm"] == 70.0 and b.get("object")["name"] == "candle"
    assert not [a for a in b.assumptions if a["slot"] == "object" and a["kind"] == "default"]
    b = agents.run_turn(agents.run_turn(text="pick up 70 mm glass cups"), action="defaults")
    assert "I assumed the glass cups weigh 100 g each." in [a["text"] for a in b.assumptions]
    # jars are a known object (65 mm, 300 g glass jars): sizes the person gives replace Morph's
    o = agents.run_turn(text="a robot that picks up 450 g jars").get("object")
    assert (o["name"], o["key"], o["diameter_mm"], o["mass_g"]) == ("glass jar", "jar", 65, 450.0)


def test_printer_bed_repair(monkeypatch):
    b = agents.run_turn(text="a 6 axis dynamixel arm with a custom pcb that moves eggs")
    b = agents.run_turn(b, action="defaults")   # the custom-PCB base shell is 239 mm: it prints on the 250 mm bed
    assert b.changes["build.config"] == "prototype" and not any("printer bed" in a["text"] for a in b.assumptions)
    assert B.deep_check(b.changes) == []
    monkeypatch.setattr(B, "deep_check", lambda ch: ["base_shell: 253 mm exceeds the 250 mm printer bed"]
                        if ch.get("build.config") == "prototype" else [])
    b = agents.run_turn(agents.run_turn(text="a 6 axis dynamixel arm with a custom pcb that moves eggs"), action="defaults")
    assert b.changes["build.config"] == "pilot" and any("printer bed" in a["text"] for a in b.assumptions)


def test_out_of_scope_nearest_robot():
    b = agents.run_turn(text="a humanoid that folds laundry")
    assert b.status == "out_of_scope" and b.scope["nearest"]["archetype"] == "soft_goods"
    assert not b.changes
    b = agents.run_turn(b, action="defaults")   # "Build the nearest robot"
    assert b.status == "ready" and b.scope["status"] == "adjusted" and b.assumptions[0]["kind"] == "scope"
    assert b.get("object")["key"] == "socks" and b.preflight["issues"] == []
    b = agents.run_turn(text="stack 2 kg boxes")
    b = agents.run_turn(b, action="defaults")
    assert b.get("object")["mass_g"] == 500 and b.changes["arm.actuator_family"] == "dynamixel" and b.changes["arm.dof"] == 4


def test_card_badges():
    b = agents.run_turn(text="pick eggs, one every 8 s")
    rows = {r["slot"]: r for r in b.to_dict()["card"]}
    assert rows["object"]["badge"] == "you said" and rows["throughput"]["badge"] == "you said"
    assert rows["layout"]["badge"] == "default" and rows["budget"]["badge"] == "default"
    agents.run_turn(b, answers={"layout": "side_to_side"})
    rows = {r["slot"]: r for r in b.to_dict()["card"]}
    assert rows["layout"]["badge"] == "you said" and rows["layout"]["text"] == "Right side to left side"


def test_assumption_alternatives_are_params_changes():
    from morph.robot.pipeline2 import PARAMS

    keys = {p[0] for p in PARAMS}
    b = agents.run_turn(agents.run_turn(text="make me a robot arm"), action="defaults")
    alts = [x for a in b.assumptions for x in a.get("alternatives", [])]
    assert alts and all(set(x["changes"]) <= keys for x in alts)
    lay = next(a for a in b.assumptions if a.get("slot") == "layout")
    assert {"task.pick_x_mm", "task.place_y_mm"} <= set(lay["alternatives"][0]["changes"])


def test_json_round_trip():
    b = agents.run_turn(agents.run_turn(text="a robot for my lab that moves test tubes"), answers={"object": "glass_vial"})
    d = json.loads(json.dumps(b.to_dict()))
    b2 = B.Brief.from_dict(d)
    assert b2.to_dict() == b.to_dict()
    assert isinstance(b2.slots["object"], B.Slot) and b2.slots["object"].source == "user"
    assert B.Brief.from_dict({"id": "x", "status": "bogus", "slots": {"nope": {}}}).status == "asking"


def test_names(tmp_path):
    b = agents.run_turn(agents.run_turn(text="something to pick strawberries"), action="defaults")
    assert b.name == "strawberry-picker"
    (tmp_path / "strawberry-picker").mkdir()
    (tmp_path / "strawberry-picker-2").mkdir()
    assert B.suggest_name(b, str(tmp_path)) == "strawberry-picker-3"
    for text in ("help me sort socks", "a robot for my lab that moves test tubes", "I have an SO-101, can it sort strawberries?"):
        n = agents.run_turn(agents.run_turn(text=text), action="defaults").name
        assert B.NAME_RE.match(n) and not n.startswith("."), n
    assert agents.run_turn(agents.run_turn(text="help me sort socks"), action="defaults").name == "sock-sorter"


def test_drafts(tmp_path):
    home = str(tmp_path)
    b = agents.run_turn(text="something to pick strawberries")
    p = B.save_draft(b, home)
    assert p == os.path.join(os.path.realpath(home), ".briefs", b.id + ".json")
    assert B.load_draft(home, b.id).to_dict() == b.to_dict()
    for bad in ("../x", "..", "a" * 16 + "/../x", "ABCDEF0123456789", "", None, "0123456789abcdef.json"):
        assert B.draft_path(home, bad) is None and B.load_draft(home, bad) is None
    old = time.time() - 8 * 24 * 3600
    os.utime(p, (old, old))
    keep = B.save_draft(agents.run_turn(text="make me a robot arm"), home)
    assert B.cleanup_drafts(home) == 1 and not os.path.exists(p) and os.path.exists(keep)
    assert B.delete_draft(home, os.path.basename(keep)[:-5]) and not os.listdir(os.path.join(home, ".briefs"))


def test_build_payload(tmp_path):
    b = agents.run_turn(text="make me a robot arm")
    with pytest.raises(ValueError):
        B.build_payload(b)
    b = agents.run_turn(b, action="defaults")
    pay = B.build_payload(b, name="my-arm")
    assert pay["name"] == "my-arm" and pay["changes"]["name"] == "my-arm" and pay["design"]["name"] == "my-arm"
    assert pay["meta"] == {"kind": "robot", "source": "", "need": b.summary[:300], "origin": "brief", "demo": False, "brief_id": b.id}
    assert Design.from_dict(pay["design"]).to_dict() == Design().with_changes(pay["changes"]).to_dict()
    assert B.build_payload(b, name="../evil")["name"] == b.name   # a bad name falls back to the suggestion
    B.mark_built(b, "my-arm", "r001")
    p = B.write_final(b, str(tmp_path))
    with open(p, encoding="utf-8") as fh:
        assert json.load(fh)["status"] == "built"
