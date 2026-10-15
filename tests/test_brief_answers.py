"""The New robot conversation as people type it (found by driving the Studio's dialog): typed replies to the questions,
the wording of questions, summaries and assumptions, and what the timeline says. Offline: no keys, no network."""

import os
import re

import pytest

from morph.studio import agents
from morph.studio import brief as B


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)


def typed(request: str, reply: str):
    b = agents.run_turn(text=request)
    assert b.status == "asking"
    return agents.run_turn(b, text=reply)


def test_the_placeholder_example_answers_the_questions_without_losing_the_object():
    # the answer box suggests "6 cm, 200 g, 10 per minute, people nearby": every part of it counts, and the strawberries
    # stay strawberries (their 3 N damage force and the force-sensing finger come with them)
    b = typed("something to pick strawberries", "6 cm, 50 g, 10 per minute, people nearby")
    o = b.get("object")
    assert (o["name"], o["key"], o["diameter_mm"], o["mass_g"], o["max_force_n"]) == ("strawberry", "strawberry", 60.0, 50.0, 3)
    assert b.get("throughput") == 6.0 and b.get("people_nearby") == "yes"
    assert b.status == "ready" and b.changes["finger.sensor"] == "fsr402" and "estop" in b.changes["electronics.extras"]
    row = next(r for r in b.to_dict()["card"] if r["slot"] == "object")
    assert row["text"].startswith("Strawberries (Ø60 mm, 50 g)") and row["badge"] == "you said"


def test_sizes_alone_change_the_object_being_discussed():
    b = typed("help me sort socks", "they weigh 80 g")
    assert (b.get("object")["name"], b.get("object")["mass_g"]) == ("rolled pair of socks", 80.0)
    b = typed("a robot that waters my plants", "300 g")       # the default bottle, heavier: still the bottle
    assert b.get("object")["mass_g"] == 300.0 and "bottle" in b.get("object")["name"]
    b = typed("I need a robot", "60 mm, 120 g")               # nothing named yet: a plain object of that size
    assert (b.get("object")["diameter_mm"], b.get("object")["mass_g"]) == (60.0, 120.0)
    b = typed("I need a robot", "eggs, 5 cm")                 # named and sized in one reply
    assert (b.get("object")["name"], b.get("object")["diameter_mm"], b.get("object")["max_force_n"]) == ("raw egg", 50.0, 25)


@pytest.mark.parametrize("reply, slot, value", [
    ("4 s", "throughput", 4.0), ("about 5 seconds", "throughput", 5.0), ("one every 8 s", "throughput", 8.0),
    ("people work next to it", "people_nearby", "yes"), ("near people", "people_nearby", "yes"),
    ("nobody nearby", "people_nearby", "no"), ("no one works next to it", "people_nearby", "no"),
    ("it's in a lab", "environment", "lab"), ("sort them into bins", "task", "sort"), ("just 1 unit", "quantity", 1),
])
def test_typed_replies(reply, slot, value):
    b = typed("I need a robot", reply)
    assert b.get(slot) == value


def test_left_to_right_is_kept_the_way_it_was_said():
    assert B.parse("from the left side to the right side")["inferred"]["layout"] == "left_to_right"
    assert B.parse("move them right to left")["inferred"]["layout"] == "side_to_side"
    b = agents.run_turn(typed("help me sort socks", "from the left side to the right side, one every 6 s"), action="defaults")
    assert "from its left side" in b.summary and "to its right side" in b.summary
    lay = B.LAYOUTS["left_to_right"]
    assert (lay["pick"], lay["place"]) == (B.LAYOUTS["side_to_side"]["place"], B.LAYOUTS["side_to_side"]["pick"])
    chips = [c["value"] for c in B.make_question(agents.run_turn(text="help me sort socks"), "layout")["chips"]]
    assert "left_to_right" not in chips  # offered only when someone says it


def test_questions_read_right_for_one_thing():
    q = {x["slot"]: x["text"] for x in agents.run_turn(text="a robot that waters my plants").questions}
    assert q["layout"] == "Where does it pick up the bottle, and where does it go?"
    q = {x["slot"]: x["text"] for x in agents.run_turn(text="help me sort socks").questions}
    assert q["layout"].endswith("and where do they go?")


def test_plants_and_pets_get_set_down_by_the_plant_or_the_bowl_not_in_a_bin():
    b = agents.run_turn(agents.run_turn(text="a robot that waters my plants"), action="defaults")
    assert "to the plant on its left" in b.summary and "bin" not in b.summary
    chips = [c["label"] for c in B.make_question(b, "layout")["chips"]]
    assert chips[0] == "Tray in front, plant on the left"
    b = agents.run_turn(agents.run_turn(text="a cat feeder"), action="defaults")
    assert "to the feeding spot on its left" in b.summary and "bin" not in b.summary
    assert not any("bin" in a.get("text", "") for a in b.assumptions)
    b = agents.run_turn(agents.run_turn(text="a robot that waters my plants"), answers={"object": "spray_bottle"})
    assert any("carries the bottle to the plant" in a["text"] for a in agents.run_turn(b, action="defaults").assumptions)


def test_assumptions_say_what_was_assumed():
    b = agents.run_turn(agents.run_turn(text="I need a robot"), answers={"object": "raw egg"}, action="defaults")
    texts = [a["text"] for a in b.assumptions]
    assert "I assumed it works at a kitchen counter, so it has an emergency stop." in texts  # nobody said kitchen
    b = agents.run_turn(agents.run_turn(text="pick eggs in my kitchen"), action="defaults")
    assert "It works at a kitchen counter, so it has an emergency stop." in [a["text"] for a in b.assumptions]
    budget = next(a["text"] for a in b.assumptions if a["slot"] == "budget")
    card = next(r for r in b.to_dict()["card"] if r["slot"] == "budget")
    assert budget.startswith("I assumed a parts budget of up to $500,") and card["text"] == "Up to $500"  # one number


def test_a_named_preset_says_its_sizes_are_morphs():
    rows = {r["slot"]: r for r in agents.run_turn(text="help me sort socks").to_dict()["card"]}
    assert rows["object"]["text"].endswith("; sizes from Morph's preset") and rows["object"]["badge"] == "you said"
    rows = {r["slot"]: r for r in typed("help me sort socks", "they weigh 80 g").to_dict()["card"]}
    assert rows["object"]["text"].endswith("; width from Morph's preset")
    rows = {r["slot"]: r for r in agents.run_turn(text="a robot that picks up 450 g candles").to_dict()["card"]}
    assert rows["object"]["text"] == "Candles (Ø45 mm, 450 g); width assumed"
    rows = {r["slot"]: r for r in agents.run_turn(text="a robot that picks up 450 g jars").to_dict()["card"]}
    assert rows["object"]["text"] == "Glass jars (Ø65 mm, 450 g); width from Morph's preset"


def test_the_timeline_never_says_zero_facts():
    events = []
    agents.run_turn(text="a robot that waters my plants", on_event=events.append)
    designer = next(e["text"] for e in events if e["role"] == "designer")
    assert designer == "Built-in rules: the kind of job only, no sizes or details yet (no Claude key)"
    events = []
    agents.run_turn(text="pick 60 g eggs every 5 s", on_event=events.append)
    assert next(e["text"] for e in events if e["role"] == "designer").startswith("Built-in rules: 2 facts from your words")


@pytest.mark.parametrize("text", ["pick up a raw egg without cracking it", "Pick a raw egg and place it without cracking it.",
                                  "pick up eggs without breaking them", "handle eggs gently, they crack easily"])
def test_saying_an_egg_is_fragile_keeps_the_eggs_measured_damage_force(text):
    # the README's example and the egg-picker's description: 'fragile' (3 N, a strawberry's) once overrode the egg's 25 N
    # while the card showed 25 N; no grip both holds a 60 g egg and stays under 2 N, so that robot failed its pick and place
    b = agents.run_turn(text=text)
    if b.status == "asking":
        b = agents.run_turn(b, action="defaults")
    assert b.status == "ready" and b.changes["object.max_force_n"] == 25 and b.preflight["issues"] == []
    assert "damaged above 25 N" in next(r["text"] for r in b.to_dict()["card"] if r["slot"] == "object")
    assert B.damage_force(b) == (25, "Morph's preset for eggs")


def test_a_stated_fragility_gives_an_object_without_one_its_damage_force_and_says_so():
    b = agents.run_turn(agents.run_turn(text="pick up 200 g glass jars without breaking them"), action="defaults")
    assert b.changes["object.max_force_n"] == 3.0
    row = next(r["text"] for r in b.to_dict()["card"] if r["slot"] == "object")
    assert "damaged above 3 N (Morph's reading of 'without breaking')" in row
    a = next(a for a in b.assumptions if a.get("slot") == "fragility")
    assert a["text"] == "I read 'without breaking' as fragile: damaged above about 3 N per finger."
    assert [o["value"] for o in a["options"]] == ["robust", "care"]
    # a 200 g jar slips below 2.6 N and is damaged above 3 / 1.5 N: the issue says where both numbers come from
    issue, = b.preflight["issues"]
    assert issue.startswith("no safe grip force: holding needs 2.60 N but the limit is 2.00 N (the glass jar's 3 N damage "
                            "force / 1.5)") and issue.endswith("the 3 N damage force is Morph's reading of 'without breaking'")
    b = agents.run_turn(b, answers={"fragility": "robust"})          # the card's option: it takes a firm squeeze
    assert b.changes["object.max_force_n"] == 0.0 and b.preflight["issues"] == []


@pytest.mark.parametrize("text, note, obj", [
    ("help my grandma open jars", "Opening jars", "glass jar"), ("open jars", "Opening jars", "glass jar"),
    ("twist the lids off jars", "Unscrewing", "glass jar"), ("unscrew bottle caps", "Unscrewing", "330 ml bottle"),
    ("assemble lego", "Assembling", "Duplo-size brick"), ("insert 50 ml tubes into a rack", "Assembling", "50 ml centrifuge tube"),
])
def test_a_job_the_gripper_cant_do_reads_as_adjusted_with_a_plain_note(text, note, obj):
    # these read "Inside what Morph builds" while the robot silently dropped the opening, twisting or assembling
    b = agents.run_turn(text=text)
    assert b.scope["status"] == "adjusted" and any(n.startswith(note) for n in b.scope["notes"]), b.scope
    b = agents.run_turn(b, action="defaults")
    assert b.status == "ready" and b.scope["status"] == "adjusted" and b.preflight["issues"] == []
    assert b.changes["object.name"] == obj and any(a["kind"] == "scope" and a["text"].startswith(note) for a in b.assumptions)


@pytest.mark.parametrize("text", ["move open-top bins to the left", "put the parts into an open box", "pick up open cans and move them",
                                  "sort screws in bins", "pick strawberries into the open tray", "a robot that sorts socks",
                                  # the open boxes are the subject of "arrive": they are open, nobody opens them
                                  "open boxes arrive on the left; move the cans into them", "open jars are on the shelf",
                                  # twist ties, screw caps, brass inserts and plugs are things to move; an assembly line is a place
                                  "sort twist ties", "move twist-off caps to the tray", "sort screw caps",
                                  "pick up screw caps", "sort brass inserts", "sort threaded inserts into bins",
                                  "move the insert to the jig", "sort plugs into bins", "pick parts off the assembly line",
                                  "move boxes to the assembly area", "move pre-assembled kits to the shelf"])
def test_wording_that_opens_twists_or_assembles_nothing_stays_inside(text):
    assert B.adjusted_kinds(text) == []


@pytest.mark.parametrize("text, kind", [
    ("open jars", "opening"), ("help my grandma open jars", "opening"), ("open boxes and take out the parts", "opening"),
    ("can it open bottles", "opening"), ("pry open paint tins", "opening"), ("take the lids off the tubs", "opening"),
    ("twist the lids off jars", "twisting"), ("twist off bottle caps", "twisting"), ("unscrew bottle caps", "twisting"),
    ("screw caps onto bottles", "twisting"), ("screw the lids on", "twisting"), ("tighten the bolts", "twisting"),
    ("assemble lego", "assembly"), ("help with assembly", "assembly"), ("insert 50 ml tubes into a rack", "assembly"),
    ("a robot that inserts pins into a board", "assembly"), ("plug cables into sockets", "assembly"),
    ("press bearings into housings", "assembly"), ("snap them together", "assembly"),
])
def test_wording_that_opens_twists_or_assembles_something_reads_as_that_job(text, kind):
    assert [k for k, _ in B.adjusted_kinds(text)] == [kind]


@pytest.mark.parametrize("text, hand_over, near", [
    # a person receives it: hand-over, with that person next to the robot
    ("pass my dad his pills", True, True), ("bring me a can of soda", True, True), ("pass them to me", True, True),
    ("give it back to her", True, True), ("hand the cup to my mum", True, True), ("bring the mugs over to us", True, True),
    # "them" and "it" are the things, and a station, a line or a crate is no person: a pick and place
    ("pick up cans and pass them to the packing station", False, False), ("give them to the sorting line", False, False),
    ("bring it to the bin", False, False), ("pass the parts to the next station", False, False),
    # a person's place is no person: "her" before a noun, or 's, owns the place the things go to
    ("bring the mail to her desk", False, False), ("move the books and bring them to her room", False, False),
    ("give the parts to my dad's workshop", False, False), ("pass the plates to my mum's kitchen table", False, False),
    ("bring the mail to my mum’s desk", False, False), ("bring the cups to his office", False, False),
    ("bring the tray to her bedside table", False, False), ("bring the laundry to her bedroom", False, False),
    # the pills are his and go to the tray, but they are his pills: he is next to the robot
    ("pass my dad's pills to the tray", False, True),
    # before anything that isn't a place, furniture or a container, "her", "his" and 's name the receiver: a missed
    # hand-over drops the emergency stop beside a person
    ("give it to her every morning", True, True), ("bring the tea to her, please", True, True),
    ("hand the tools to her when she asks", True, True), ("bring her the cup", True, True),
    ("a robot that brings the meds to her twice a day", True, True),
    ("a robot that brings water to her because she can't walk", True, True),
    ("a robot that gives the medicine to her nurse", True, True), ("bring breakfast to her bedside every day", True, True),
    ("give it to her husband", True, True), ("bring the remote to my dad's friend", True, True),
    ("give the medicine to his nurse", True, True), ("bring the cup to her at her desk", True, True),
    # a place word in front of a person word describes the person: the place is hers only when it ends the noun phrase
    ("a robot that brings the medicine to her home nurse", True, True), ("bring the pills to her care home nurse", True, True),
    ("bring the tea to my dad's room mate", True, True), ("bring water to her kitchen helper", True, True),
    ("hand the files to her office manager", True, True), ("bring the coffee to her desk neighbour", True, True),
    ("bring the parcel to her house guest", True, True), ("give the keys to his van driver", True, True),
    ("give the report to his lab partner", True, True), ("hand the tools to his workshop assistant", True, True),
    ("give the meds to my mum's home carer", True, True), ("hand the cup to her table neighbour", True, True),
    ("give the forms to his line manager", True, True), ("pass the tray to her machine operator", True, True),
    # ... and the place is hers when nothing but a function word, a time or manner word, a number or punctuation follows
    ("bring the cup to her old wooden kitchen table", False, False), ("bring the meds to her bedroom twice a day", False, False),
    ("bring the medicine to her home", False, False), ("bring the cup to her room so she can drink it", False, False),
    ("bring the mail to her desk downstairs", False, False), ("bring the mail to her desk drawer", False, False),
    ("bring the post to her room's door", False, False), ("bring the tray to her room 12", False, False),
    ("bring the mail to her desk daily", False, False), ("pass the cups to his kitchen counter.", False, False),
    ("give the cans to his packing station", False, False), ("give the bolts to his assembly line", False, False),
    ("pass the panels to his conveyor", False, False), ("bring the parts to his machine", False, False),
])
def test_a_hand_over_goes_to_a_person(text, hand_over, near):
    assert bool(re.search(B.HAND_TO_PERSON, text)) is hand_over
    assert bool(re.search(B.PERSON_NEAR, text)) is near


@pytest.mark.parametrize("text, task, estop", [("pick up cans and pass them to the packing station", "pick_place", False),
                                               ("pick up cans and pass them to me", "hand_over", True),
                                               ("move the books and bring them to her room", "pick_place", False),
                                               # a desk is an office, whose robots work beside people: its own e-stop
                                               ("bring the mail to her desk", "pick_place", True)])
def test_passing_things_on_to_a_station_is_a_pick_and_place(text, task, estop):
    # "pass them to the packing station" built a hand-over with an emergency stop, summarised as "hands over" cans; so did
    # "bring them to her room" (her room is no person)
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert B.value(b, "task") == task and ("estop" in b.changes["electronics.extras"]) is estop
    assert ("hands over" in b.summary) is (task == "hand_over")


@pytest.mark.parametrize("text", [
    "a robot that brings the meds to her twice a day", "a robot that brings water to her because she can't walk",
    "a robot that gives the medicine to her nurse", "bring breakfast to her bedside every day", "give it to her husband",
    "bring the remote to my dad's friend", "pass my dad his pills", "bring me a can of soda", "pass them to me",
    "give it back to her", "hand the cup to my mum", "bring the mugs over to us", "give it to her every morning",
    "bring the tea to her, please", "hand the tools to her when she asks", "bring her the cup",
    "pick up cans and pass them to me",
    # a place word describing the person ("her home nurse") is no place of hers
    "a robot that brings the medicine to her home nurse", "a robot that brings the pills to her care home nurse",
    "a robot that brings the tea to my dad's room mate", "a robot that brings water to her kitchen helper",
    "a robot that hands the files to her office manager", "a robot that brings the coffee to her desk neighbour",
    "a robot that brings the parcel to her house guest", "a robot that gives the keys to his van driver",
    "a robot that gives the report to his lab partner"])
def test_a_carer_brief_hands_over_with_people_nearby_and_an_emergency_stop(text):
    # "to her twice a day", "to her nurse" and "to my dad's friend" built a pick and place with no e-stop: the possessive
    # guard took any word after "her" for the place it owned; then "to her home nurse", "to her care home nurse" and "to
    # my dad's room mate" did: it took a place word in front of the person for her place
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert B.value(b, "task") == "hand_over" and B.value(b, "people_nearby") == "yes"
    assert "estop" in b.changes["electronics.extras"] and re.search(r"\bhands (?:over|you)\b", b.summary), b.summary


@pytest.mark.parametrize("text", ["a 4 dof arm with hobby servos to pick up ping pong balls",
                                  "a 4 dof arm with hobby servos to move 40 g wooden blocks 50 mm wide"])
def test_hobby_servos_that_cant_grip_the_object_safely_become_smart_bus_servos(text):
    # a hobby gripper servo has no torque limit: its force-sensing close can't reach a ping pong ball at the small end of
    # its size without risking stall when a sensor reads nothing, and without force sensors its position-only close holds
    # a 50 mm block 8 % over its nominal size at stall (plan issues), so New robot's repair uses smart-bus servos, as it
    # does when hobby servos can't hold the load, and says so
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert b.preflight["issues"] == [] and b.changes["arm.actuator_family"] == "smart_bus"
    assert any(a["kind"] == "repair" and a["text"].startswith("A hobby PWM gripper servo has no torque limit")
               for a in b.assumptions)


@pytest.mark.parametrize("text", ["open boxes arrive on the left; move the cans into them", "sort twist ties"])
def test_a_request_that_only_names_open_or_twisted_things_stays_inside(text):
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert b.scope["status"] != "adjusted" and b.scope["notes"] == [], b.scope


@pytest.mark.parametrize("text, estop", [("help my grandma open jars", True), ("help my grandma move her mugs", True),
                                         ("pass my dad his pills", True), ("sort socks for me", True), ("sort socks", False),
                                         ("open jars", False)])
def test_a_person_the_robot_works_for_is_next_to_it_so_it_gets_an_emergency_stop(text, estop):
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert ("estop" in b.changes["electronics.extras"]) is estop
    people = next(a["text"] for a in b.assumptions if a["slot"] == "people_nearby")
    assert people == ("I assumed people work next to it, so it has an emergency stop." if estop else
                      "I assumed it works on its own, so there is no emergency stop.")


def test_pass_my_dad_his_pills_hands_over_a_pill_bottle():
    b = agents.run_turn(agents.run_turn(text="pass my dad his pills"), action="defaults")
    assert b.scope["notes"] == ["Single pills (5 to 15 mm) are far smaller than the 26 mm this gripper closes on; the robot can "
                                "move the pill bottle (35 mm) instead."]          # it said screws and nuts
    assert (B.value(b, "task"), b.changes["object.name"]) == ("hand_over", "pill bottle")
    assert b.changes["electronics.extras"] == "estop" and "hands over pill bottles" in b.summary   # no sorting camera


def test_every_width_and_weight_note_gives_the_grippers_real_limit():
    from morph.studio import capability as cap

    lego = B.parse("assemble lego")["implicit"][0]["note"]
    assert f"narrower than the {cap.min_width('box'):g} mm" in lego                      # a brick is flat-sided: 21.5 mm
    for text in ("sort grapes", "move test tubes", "sort pills"):
        assert f"the {cap.min_width('cylinder'):g} mm" in B.parse(text)["implicit"][0]["note"], text
    assert B.unfit_note(B.object_value("water bottle")) == ("Water bottles (65 mm, 520 g) are too heavy: a desk arm here lifts "
                                                            "500 g at most.")             # it said "outside 26 to 75 mm wide"
